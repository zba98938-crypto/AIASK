"""Thin provider adapters over the existing AIASK data source layer."""

from __future__ import annotations

from typing import Any

from .base import ProviderContractFetchResult, ProviderContractMeta, dedupe_text
from .models import (
    EquityHistorical,
    EquityHistoricalBar,
    EquityHistoricalQuery,
    EquityQuote,
    EquityQuoteQuery,
    StockInfo,
    StockInfoQuery,
    TradingCalendar,
    TradingCalendarQuery,
)


def _default_provider():
    from ..data_source import data_source

    return data_source


def _source_chain(result: Any, default: str) -> list[str]:
    if isinstance(result, dict):
        chain = result.get("source_chain")
        if isinstance(chain, list):
            return dedupe_text(chain)
        return dedupe_text(
            [
                result.get("backend_requested"),
                result.get("backend_used"),
                result.get("source"),
                default,
            ]
        )
    if isinstance(result, list):
        sources = [item.get("source") for item in result if isinstance(item, dict)]
        return dedupe_text(sources or [default])
    return [default]


def _provider_used(result: Any, source_chain: list[str], default: str) -> str:
    if isinstance(result, dict):
        return str(result.get("backend_used") or result.get("source") or (source_chain[-1] if source_chain else default))
    return source_chain[-1] if source_chain else default


def _meta(
    *,
    model: str,
    result: Any,
    default_source: str,
    data_timestamp: str | None = None,
) -> ProviderContractMeta:
    chain = _source_chain(result, default_source)
    provider = _provider_used(result, chain, default_source)
    return ProviderContractMeta(
        standard_model=model,
        provider_requested=chain[0] if chain else default_source,
        provider_used=provider,
        source_chain=chain,
        fallback_used=bool(isinstance(result, dict) and result.get("fallback_used")) or len(chain) > 1,
        fallback_reason=result.get("fallback_reason") if isinstance(result, dict) else None,
        data_timestamp=data_timestamp,
        freshness={
            "source_timestamp": data_timestamp,
        }
        if data_timestamp
        else {},
        quality={
            "status": "available" if result else "failed",
            "quality_flags": list(result.get("quality_flags") or []) if isinstance(result, dict) else [],
        },
    )


def fetch_equity_quote(query: EquityQuoteQuery | dict[str, Any], *, provider: Any | None = None) -> ProviderContractFetchResult:
    q = query if isinstance(query, EquityQuoteQuery) else EquityQuoteQuery.model_validate(query)
    provider = provider or _default_provider()
    raw = provider.get_realtime_quote(q.resolved_code)
    if not raw:
        meta = _meta(model="EquityQuote", result={}, default_source="data_source.get_realtime_quote")
        return ProviderContractFetchResult(success=False, model="EquityQuote", error="quote not found", meta=meta)
    data_timestamp = str(raw.get("data_timestamp") or raw.get("time") or raw.get("trade_time") or "") or None
    meta = _meta(model="EquityQuote", result=raw, default_source="data_source.get_realtime_quote", data_timestamp=data_timestamp)
    data = EquityQuote(
        code=str(raw.get("code") or q.resolved_code),
        name=raw.get("name"),
        price=raw.get("price"),
        change=raw.get("change"),
        changePercent=raw.get("changePercent") or raw.get("change_pct"),
        open=raw.get("open"),
        high=raw.get("high"),
        low=raw.get("low"),
        preClose=raw.get("preClose") or raw.get("pre_close"),
        volume=raw.get("volume"),
        amount=raw.get("amount"),
        tradeTime=raw.get("trade_time") or raw.get("time"),
        provider=meta,
    )
    return ProviderContractFetchResult(success=True, model="EquityQuote", data=data.model_dump(mode="json", by_alias=True), meta=meta)


def fetch_equity_historical(query: EquityHistoricalQuery | dict[str, Any], *, provider: Any | None = None) -> ProviderContractFetchResult:
    q = query if isinstance(query, EquityHistoricalQuery) else EquityHistoricalQuery.model_validate(query)
    provider = provider or _default_provider()
    rows = provider.get_kline(q.resolved_code, q.period, q.limit)
    if not rows:
        meta = _meta(model="EquityHistorical", result=[], default_source="data_source.get_kline")
        return ProviderContractFetchResult(success=False, model="EquityHistorical", error="kline not found", meta=meta)
    latest_date = str((rows[-1] if rows else {}).get("date") or "") or None
    meta = _meta(model="EquityHistorical", result=rows, default_source="data_source.get_kline", data_timestamp=latest_date)
    data = EquityHistorical(
        code=q.resolved_code,
        period=q.period,
        rows=[EquityHistoricalBar.model_validate(dict(item)) for item in rows],
        provider=meta,
    )
    return ProviderContractFetchResult(success=True, model="EquityHistorical", data=data.model_dump(mode="json"), meta=meta)


def fetch_stock_info(query: StockInfoQuery | dict[str, Any], *, provider: Any | None = None) -> ProviderContractFetchResult:
    q = query if isinstance(query, StockInfoQuery) else StockInfoQuery.model_validate(query)
    provider = provider or _default_provider()
    fetcher = getattr(provider, "get_stock_info", None) or getattr(provider, "get_more_info", None)
    raw = fetcher(q.code) if callable(fetcher) else None
    if not raw:
        meta = _meta(model="StockInfo", result={}, default_source="data_source.get_stock_info")
        return ProviderContractFetchResult(success=False, model="StockInfo", error="stock info not found", meta=meta)
    meta = _meta(model="StockInfo", result=raw, default_source="data_source.get_stock_info")
    data = StockInfo(
        code=str(raw.get("code") or raw.get("symbol") or q.code),
        name=raw.get("name") or raw.get("stock_name"),
        industry=raw.get("industry"),
        listDate=raw.get("listDate") or raw.get("list_date"),
        totalShares=raw.get("totalShares") or raw.get("total_shares"),
        floatShares=raw.get("floatShares") or raw.get("float_shares"),
        totalMarketCap=raw.get("totalMarketCap") or raw.get("total_market_cap"),
        floatMarketCap=raw.get("floatMarketCap") or raw.get("float_market_cap"),
        provider=meta,
    )
    return ProviderContractFetchResult(success=True, model="StockInfo", data=data.model_dump(mode="json", by_alias=True), meta=meta)


def fetch_trading_calendar(query: TradingCalendarQuery | dict[str, Any], *, provider: Any | None = None) -> ProviderContractFetchResult:
    q = query if isinstance(query, TradingCalendarQuery) else TradingCalendarQuery.model_validate(query)
    provider = provider or _default_provider()
    raw = provider.get_trading_dates(
        market=q.market,
        start_time=q.start_date or "",
        end_time=q.end_date or "",
        count=q.count,
    )
    dates = list(raw.get("data") or []) if isinstance(raw, dict) else list(raw or [])
    if not dates:
        meta = _meta(model="TradingCalendar", result=raw, default_source="data_source.get_trading_dates")
        return ProviderContractFetchResult(success=False, model="TradingCalendar", error="trading dates not found", meta=meta)
    meta = _meta(model="TradingCalendar", result=raw, default_source="data_source.get_trading_dates", data_timestamp=str(dates[-1]))
    data = TradingCalendar(market=q.market, dates=[str(item) for item in dates], count=len(dates), provider=meta)
    return ProviderContractFetchResult(success=True, model="TradingCalendar", data=data.model_dump(mode="json"), meta=meta)
