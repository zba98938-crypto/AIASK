"""Standard data models for AIASK provider contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .base import ContractBaseModel, ProviderContractMeta


class EquityQuoteQuery(ContractBaseModel):
    code: str | None = Field(default=None, description="6-digit security code.")
    symbol: str | None = Field(default=None, description="Alias for code.")

    @model_validator(mode="after")
    def require_identifier(self) -> "EquityQuoteQuery":
        if not (self.code or self.symbol):
            raise ValueError("code or symbol is required")
        return self

    @property
    def resolved_code(self) -> str:
        return str(self.code or self.symbol or "").strip()


class EquityQuote(ContractBaseModel):
    code: str
    name: str | None = None
    price: float | None = None
    change: float | None = None
    change_percent: float | None = Field(default=None, alias="changePercent")
    open: float | None = None
    high: float | None = None
    low: float | None = None
    pre_close: float | None = Field(default=None, alias="preClose")
    volume: int | float | None = None
    amount: float | None = None
    trade_time: str | None = Field(default=None, alias="tradeTime")
    provider: ProviderContractMeta


class EquityHistoricalQuery(ContractBaseModel):
    code: str | None = Field(default=None, description="6-digit security code.")
    symbol: str | None = Field(default=None, description="Alias for code.")
    period: Literal["daily", "weekly", "monthly", "1m", "5m", "15m", "30m", "60m"] = "daily"
    limit: int = Field(default=100, ge=1, le=5000)
    start_date: str | None = Field(default=None, description="YYYY-MM-DD or YYYYMMDD.")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD or YYYYMMDD.")

    @model_validator(mode="after")
    def require_identifier(self) -> "EquityHistoricalQuery":
        if not (self.code or self.symbol):
            raise ValueError("code or symbol is required")
        return self

    @property
    def resolved_code(self) -> str:
        return str(self.code or self.symbol or "").strip()


class EquityHistoricalBar(ContractBaseModel):
    date: str
    open: float | None = None
    close: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | float | None = None
    amount: float | None = None
    turnover: float | None = None
    change_pct: float | None = None
    source: str | None = None


class EquityHistorical(ContractBaseModel):
    code: str
    period: str
    rows: list[EquityHistoricalBar] = Field(default_factory=list)
    provider: ProviderContractMeta


class StockInfoQuery(ContractBaseModel):
    code: str


class StockInfo(ContractBaseModel):
    code: str
    name: str | None = None
    industry: str | None = None
    list_date: str | None = Field(default=None, alias="listDate")
    total_shares: str | float | None = Field(default=None, alias="totalShares")
    float_shares: str | float | None = Field(default=None, alias="floatShares")
    total_market_cap: str | float | None = Field(default=None, alias="totalMarketCap")
    float_market_cap: str | float | None = Field(default=None, alias="floatMarketCap")
    provider: ProviderContractMeta


class TradingCalendarQuery(ContractBaseModel):
    market: str = Field(default="SH", description="Exchange market, such as SH or SZ.")
    count: int = Field(default=-1, ge=-1)
    start_date: str | None = Field(default=None, description="YYYYMMDD.")
    end_date: str | None = Field(default=None, description="YYYYMMDD.")


class TradingCalendar(ContractBaseModel):
    market: str
    dates: list[str] = Field(default_factory=list)
    count: int
    provider: ProviderContractMeta


class FinancialMetricsQuery(ContractBaseModel):
    code: str | None = Field(default=None, description="6-digit security code.")
    stock_code: str | None = Field(default=None, description="Deprecated alias for code.")
    symbol: str | None = Field(default=None, description="Alias for code.")
    ticker: str | None = Field(default=None, description="Alias for code.")


class FinancialMetrics(ContractBaseModel):
    code: str | None = None
    report_date: str | None = Field(default=None, alias="reportDate")
    revenue: float | None = None
    net_profit: float | None = Field(default=None, alias="netProfit")
    gross_profit_margin: float | None = Field(default=None, alias="grossProfitMargin")
    net_profit_margin: float | None = Field(default=None, alias="netProfitMargin")
    roe: float | None = None
    roa: float | None = None
    debt_ratio: float | None = Field(default=None, alias="debtRatio")
    current_ratio: float | None = Field(default=None, alias="currentRatio")
    eps: float | None = None
    bvps: float | None = None
    revenue_growth: float | None = Field(default=None, alias="revenueGrowth")
    profit_growth: float | None = Field(default=None, alias="profitGrowth")
    source: str | None = None
    provider: ProviderContractMeta | None = None


class StockFundFlowQuery(ContractBaseModel):
    code: str | None = Field(default=None, description="6-digit security code.")
    stock_code: str | None = Field(default=None, description="Deprecated alias for code.")
    symbol: str | None = Field(default=None, description="Alias for code.")
    ticker: str | None = Field(default=None, description="Alias for code.")
    prefer_db: bool = True


class StockFundFlow(ContractBaseModel):
    code: str | None = None
    name: str | None = None
    main_net_inflow: float | None = Field(default=None, alias="mainNetInflow")
    main_inflow_percent: float | None = Field(default=None, alias="mainInflowPercent")
    super_large_net_inflow: float | None = Field(default=None, alias="superLargeNetInflow")
    large_net_inflow: float | None = Field(default=None, alias="largeNetInflow")
    middle_net_inflow: float | None = Field(default=None, alias="middleNetInflow")
    small_net_inflow: float | None = Field(default=None, alias="smallNetInflow")
    trade_date: str | None = Field(default=None, alias="tradeDate")
    source: str | None = None
    provider: ProviderContractMeta | None = None


class NorthFundFlowQuery(ContractBaseModel):
    days: int = Field(default=30, ge=1, le=500)


class NorthFundFlowItem(ContractBaseModel):
    date: str | None = None
    sh_connect: float | None = Field(default=None, alias="shConnect")
    sz_connect: float | None = Field(default=None, alias="szConnect")
    total: float | None = None
    sh_cumulative: float | None = Field(default=None, alias="shCumulative")
    sz_cumulative: float | None = Field(default=None, alias="szCumulative")
    cumulative: float | None = None


class NorthFundFlow(ContractBaseModel):
    items: list[NorthFundFlowItem] = Field(default_factory=list)
    source: str | None = None
    stale: bool | None = None
    stale_age_days: int | None = None
    partial: bool | None = None
    message: str | None = None
    provider: ProviderContractMeta | None = None


class NorthFundHoldingQuery(ContractBaseModel):
    stock_code: str = Field(description="6-digit security code.")


class NorthFundHolding(ContractBaseModel):
    shares: float | None = None
    ratio: float | None = None
    change: float | None = None
    trade_date: str | None = None
    source: str | None = None
    provider: ProviderContractMeta | None = None


class NorthFundTopQuery(ContractBaseModel):
    top_n: int = Field(default=20, ge=1, le=500)


class NorthFundTop(ContractBaseModel):
    code: str | None = None
    name: str | None = None
    shares: float | None = None
    ratio: float | None = None
    market_cap: float | None = Field(default=None, alias="marketCap")


class SectorFundFlowQuery(ContractBaseModel):
    top_n: int = Field(default=20, ge=1, le=500)


class SectorFundFlow(ContractBaseModel):
    name: str | None = None
    change_percent: float | None = Field(default=None, alias="changePercent")
    main_net_inflow: float | None = Field(default=None, alias="mainNetInflow")
    main_net_inflow_percent: float | None = Field(default=None, alias="mainNetInflowPercent")
    super_large_net_inflow: float | None = Field(default=None, alias="superLargeNetInflow")
    large_net_inflow: float | None = Field(default=None, alias="largeNetInflow")
    medium_net_inflow: float | None = Field(default=None, alias="mediumNetInflow")
    small_net_inflow: float | None = Field(default=None, alias="smallNetInflow")
    source: str | None = None


class ConceptFundFlowQuery(SectorFundFlowQuery):
    pass


class ConceptFundFlow(SectorFundFlow):
    inflow: float | None = None
    outflow: float | None = None


class DragonTigerQuery(ContractBaseModel):
    date: str = Field(default="", description="YYYY-MM-DD or YYYYMMDD. Defaults to latest trading date.")
    stock_code: str = Field(default="", description="Optional 6-digit security code.")


class DragonTiger(ContractBaseModel):
    code: str | None = None
    name: str | None = None
    close_price: float | None = Field(default=None, alias="closePrice")
    change_percent: float | None = Field(default=None, alias="changePercent")
    reason: str | None = None
    buy_amount: float | None = Field(default=None, alias="buyAmount")
    sell_amount: float | None = Field(default=None, alias="sellAmount")
    net_amount: float | None = Field(default=None, alias="netAmount")
    source: str | None = None


class MarginDataQuery(ContractBaseModel):
    stock_code: str = Field(default="", description="Optional 6-digit security code.")
    days: int = Field(default=30, ge=1, le=200)


class MarginData(ContractBaseModel):
    date: str | None = None
    code: str | None = None
    name: str | None = None
    margin_balance: float | None = Field(default=None, alias="marginBalance")
    margin_buy: float | None = Field(default=None, alias="marginBuy")
    margin_repay: float | None = Field(default=None, alias="marginRepay")
    short_balance: float | None = Field(default=None, alias="shortBalance")
    short_sell: float | None = Field(default=None, alias="shortSell")
    short_repay: float | None = Field(default=None, alias="shortRepay")
    total_balance: float | None = Field(default=None, alias="totalBalance")
    source: str | None = None


class MarginRankingQuery(ContractBaseModel):
    top_n: int = Field(default=20, ge=1, le=500)
    sort_by: Literal["balance", "buy", "sell"] | str = "balance"


class MarginRanking(ContractBaseModel):
    date: str | None = None
    code: str | None = None
    name: str | None = None
    margin_balance: float | None = Field(default=None, alias="marginBalance")
    margin_buy: float | None = Field(default=None, alias="marginBuy")
    short_sell: float | None = Field(default=None, alias="shortSell")
    total_balance: float | None = Field(default=None, alias="totalBalance")
    source: str | None = None


class BlockTradesQuery(ContractBaseModel):
    date: str = Field(default="", description="YYYY-MM-DD or YYYYMMDD. Defaults to today.")
    stock_code: str = Field(default="", description="Optional 6-digit security code.")
    limit: int = Field(default=500, ge=1, le=1000)


class BlockTrades(ContractBaseModel):
    date: str | None = None
    code: str | None = None
    name: str | None = None
    industry: str | None = None
    price: float | None = None
    volume: float | None = None
    amount: float | None = None
    premium: float | None = None
    buyer: str | None = None
    seller: str | None = None
    data_quality: dict[str, Any] | None = Field(default=None, alias="dataQuality")


class MarketBlocksQuery(ContractBaseModel):
    block_type: Literal["industry", "concept", "region"] = "industry"
    limit: int | None = Field(default=None, ge=1, le=1000)


class MarketBlock(ContractBaseModel):
    code: str | None = None
    name: str | None = None
    block_code: str | None = Field(default=None, alias="blockCode")
    block_name: str | None = Field(default=None, alias="blockName")
    block_type: str | None = Field(default=None, alias="blockType")
    stock_count: int | None = Field(default=None, alias="stockCount")
    avg_change_pct: float | None = Field(default=None, alias="avgChangePct")
    total_amount: float | None = Field(default=None, alias="totalAmount")
    leader_code: str | None = Field(default=None, alias="leaderCode")
    leader_name: str | None = Field(default=None, alias="leaderName")


class MarketBlocks(ContractBaseModel):
    blocks: list[MarketBlock] = Field(default_factory=list)
    count: int | None = None
    block_type: str | None = None
    source: str | None = None
    degraded: bool | None = None
    fallback_reason: str | None = None
    provider: ProviderContractMeta | None = None


class BlockStocksQuery(ContractBaseModel):
    block_code: str


class BlockStock(ContractBaseModel):
    code: str | None = None
    full_code: str | None = None
    name: str | None = None
    stock_code: str | None = Field(default=None, alias="stockCode")
    stock_name: str | None = Field(default=None, alias="stockName")
    change_pct: float | None = Field(default=None, alias="changePct")
    price: float | None = None
    volume: float | None = None
    amount: float | None = None


class BlockStocks(ContractBaseModel):
    block_code: str | None = None
    block_name: str | None = None
    block_type: str | None = None
    stocks: list[BlockStock] = Field(default_factory=list)
    count: int | None = None
    source: str | None = None
    provider: ProviderContractMeta | None = None


class MacroIndicatorQuery(ContractBaseModel):
    indicator: str = Field(description="Macro indicator code, such as gdp, cpi, pmi, m2, lpr_1y.")
    limit: int = Field(default=120, ge=1, le=480)


class MacroIndicatorRecord(ContractBaseModel):
    period: str | None = None
    value: float | None = None
    yoy_change: float | None = Field(default=None, alias="yoyChange")
    mom_change: float | None = Field(default=None, alias="momChange")
    publish_date: str | None = Field(default=None, alias="publishDate")


class MacroIndicator(ContractBaseModel):
    indicator: str | None = None
    records: list[MacroIndicatorRecord] = Field(default_factory=list)
    provider: ProviderContractMeta | None = None


class OptionChainQuery(ContractBaseModel):
    underlying: str = Field(description="Underlying code or alias, such as 510050, 50ETF, 510300, 300ETF.")
    expiry_month: str = Field(default="", description="YYYY-MM or YYYYMM. Defaults to nearest month.")
    limit: int = Field(default=200, ge=1, le=1000)


class OptionContract(ContractBaseModel):
    code: str | None = None
    name: str | None = None
    type: Literal["call", "put"] | str | None = None
    expiry_month: str | None = Field(default=None, alias="expiryMonth")
    strike: float | None = None
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_volume: int | None = Field(default=None, alias="bidVolume")
    ask_volume: int | None = Field(default=None, alias="askVolume")
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = Field(default=None, alias="prevClose")
    change_percent: float | None = Field(default=None, alias="changePercent")
    volume: int | None = None
    amount: float | None = None
    open_interest: int | None = Field(default=None, alias="openInterest")
    time: str | None = None
    underlying: str | None = None


class OptionChain(ContractBaseModel):
    underlying: dict[str, Any] | None = None
    expiry_months: list[str] = Field(default_factory=list, alias="expiryMonths")
    selected_expiry: list[str] = Field(default_factory=list, alias="selectedExpiry")
    options: list[OptionContract] = Field(default_factory=list)
    truncated: bool | None = None
    source_chain: list[str] = Field(default_factory=list)
    fallback_reason: list[str] = Field(default_factory=list)
    degraded: bool | None = None
    provider: ProviderContractMeta | None = None
