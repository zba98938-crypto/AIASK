"""
数据同步 MCP 工具模块

Phase 4 实现 - 数据源整合优化

提供以下 MCP 工具：
1. sync_kline_data - 同步K线数据（带缓存）
2. sync_trading_calendar - 同步交易日历
3. batch_sync_klines - 批量同步股票K线
4. get_cache_stats - 获取缓存统计
5. clear_cache - 清空缓存
"""

import asyncio
from typing import Optional, List

from ..services.data_sync import data_sync_service, CACHE_TTL
from ..cache import cache
from ..utils import enrich_response_meta, fail, now_iso, ok, parse_date_input, validate_int_range, validate_stock_code_format


def _sync_envelope(result: dict, *, source: str) -> dict:
    payload = dict(result or {})
    payload.setdefault("source", source)
    payload.setdefault("cached", False)
    payload.setdefault("timestamp", now_iso())
    if payload.get("success", False):
        payload.setdefault("error", None)
    else:
        payload.setdefault("data", None)
    return enrich_response_meta(
        payload,
        source=str(payload.get("source") or source),
        source_chain=[str(payload.get("source") or source)],
        degraded=bool(not payload.get("success", False)),
    )


def register(mcp):
    """注册数据同步相关的 MCP 工具"""

    @mcp.tool()
    async def sync_kline_data(
        stock_code: str,
        period: str = "daily",
        start_date: str = "",
        end_date: str = "",
        limit: int = 100,
        use_cache: bool = True
    ) -> dict:
        """
        同步K线数据（带多层缓存）

        数据流向: SimpleCache → SQLite → API（DataSource/Tushare/AkShare）

        Args:
            stock_code (str, required): 股票代码，如 "600519"、"000001"
            period (str, optional): K线周期，可选 "daily"/"weekly"/"monthly"，默认 "daily"
            start_date (str, optional): 开始日期，格式 YYYYMMDD
            end_date (str, optional): 结束日期，格式 YYYYMMDD
            limit (int, optional): 数据条数限制，默认 100
            use_cache (bool, optional): 是否使用缓存，默认 True

        Returns:
            dict: {"success": bool, "data": list[dict], "source": str, "count": int, "cached": bool}
            每条K线包含: date, open, high, low, close, volume, amount

        Errors:
            - 股票代码无效或所有数据源均不可用时返回 success=false

        Examples:
            sync_kline_data("600519")
            sync_kline_data("000001", period="weekly", limit=50)
        """
        _, code_error = validate_stock_code_format(stock_code)
        if code_error:
            return _sync_envelope(fail(code_error), source="data_sync.kline")
        valid_periods = {"daily", "weekly", "monthly"}
        if str(period or "").strip() not in valid_periods:
            return _sync_envelope(
                fail(f"period 无效: {period}. 支持: {', '.join(sorted(valid_periods))}"),
                source="data_sync.kline",
            )
        limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1)
        if limit_error:
            return _sync_envelope(fail(limit_error), source="data_sync.kline")
        if start_date and parse_date_input(start_date) is None:
            return _sync_envelope(fail(f"start_date 无效: {start_date}"), source="data_sync.kline")
        if end_date and parse_date_input(end_date) is None:
            return _sync_envelope(fail(f"end_date 无效: {end_date}"), source="data_sync.kline")
        result = await data_sync_service.get_kline_with_cache(
            stock_code=stock_code,
            period=period,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            use_cache=use_cache
        )
        return _sync_envelope(result, source=str(result.get("source") or "data_sync.kline"))

    @mcp.tool()
    async def sync_trading_calendar(year: int = None) -> dict:
        """
        同步交易日历

        Args:
            year (int, optional): 年份，默认当前年份

        Returns:
            dict: {"success": bool, "data": {"dates": list[str], "count": int, "year": int}}
            dates 格式: ["YYYYMMDD", ...]

        Examples:
            sync_trading_calendar()
            sync_trading_calendar(year=2025)
        """
        result = await data_sync_service.sync_trading_dates(year)
        return _sync_envelope(result, source=str(result.get("source") or "data_sync.trading_calendar"))

    @mcp.tool()
    async def batch_sync_klines(
        codes: List[str],
        start_date: str = "",
        end_date: str = "",
        period: str = "daily"
    ) -> dict:
        """
        批量同步股票K线数据

        Args:
            codes (list[str], required): 股票代码列表，如 ["600519", "000001"]
            start_date (str, optional): 开始日期，格式 YYYYMMDD
            end_date (str, optional): 结束日期，格式 YYYYMMDD
            period (str, optional): K线周期，默认 "daily"

        Returns:
            dict: {"success": bool, "data": {"synced": int, "failed": int, "total": int, "errors": list[str]}}

        Examples:
            batch_sync_klines(["600519", "000001"])
            batch_sync_klines(["600519", "000858"], start_date="20250101")
        """
        normalized_codes = [str(code or "").strip() for code in list(codes or []) if str(code or "").strip()]
        if not normalized_codes:
            return _sync_envelope(
                fail("codes 不能为空"),
                source="data_sync.batch_sync_klines",
            )
        invalid_codes = [code for code in normalized_codes if validate_stock_code_format(code)[1] is not None]
        if invalid_codes:
            return _sync_envelope(
                fail(f"存在无效股票代码: {', '.join(invalid_codes)}"),
                source="data_sync.batch_sync_klines",
            )
        valid_periods = {"daily", "weekly", "monthly"}
        if str(period or "").strip() not in valid_periods:
            return _sync_envelope(
                fail(f"period 无效: {period}. 支持: {', '.join(sorted(valid_periods))}"),
                source="data_sync.batch_sync_klines",
            )
        if start_date and parse_date_input(start_date) is None:
            return _sync_envelope(fail(f"start_date 无效: {start_date}"), source="data_sync.batch_sync_klines")
        if end_date and parse_date_input(end_date) is None:
            return _sync_envelope(fail(f"end_date 无效: {end_date}"), source="data_sync.batch_sync_klines")

        result = await data_sync_service.sync_stock_klines(
            codes=normalized_codes,
            start_date=start_date,
            end_date=end_date,
            period=period
        )
        return _sync_envelope(result, source="data_sync.batch_sync_klines")

    @mcp.tool()
    async def sync_market_temperature_snapshot_cache(
        limit: int = 1000,
        top_n: int = 20,
        as_of: str = "",
        min_bars: int = 20,
    ) -> dict:
        """Refresh the local market-temperature snapshot cache through the data-sync surface."""
        limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1, maximum=1000)
        if limit_error:
            return _sync_envelope(fail(limit_error), source="data_sync.market_temperature_snapshot_cache")
        top_n, top_n_error = validate_int_range(top_n, field_name="top_n", minimum=0, maximum=50)
        if top_n_error:
            return _sync_envelope(fail(top_n_error), source="data_sync.market_temperature_snapshot_cache")
        min_bars, min_bars_error = validate_int_range(min_bars, field_name="min_bars", minimum=2, maximum=120)
        if min_bars_error:
            return _sync_envelope(fail(min_bars_error), source="data_sync.market_temperature_snapshot_cache")

        normalized_as_of = None
        if str(as_of or "").strip():
            parsed_as_of = parse_date_input(str(as_of))
            if parsed_as_of is None:
                return _sync_envelope(
                    fail(f"as_of invalid: {as_of}"),
                    source="data_sync.market_temperature_snapshot_cache",
                )
            normalized_as_of = parsed_as_of.isoformat()

        from . import market_temperature as market_temperature_tools

        result = await market_temperature_tools.refresh_market_temperature_snapshot_cache(
            limit=limit,
            top_n=top_n,
            as_of=normalized_as_of,
            min_bars=min_bars,
        )
        payload = dict(result or {})
        source = "data_sync.market_temperature_snapshot_cache"
        nested_chain = [
            str(item)
            for item in list(payload.get("source_chain") or (payload.get("meta") or {}).get("source_chain") or [])
            if str(item).strip()
        ]
        payload["source"] = source
        meta = dict(payload.get("meta") or {})
        meta["data_sync_source_chain"] = [source, *nested_chain]
        payload["meta"] = meta
        return _sync_envelope(payload, source=source)

    @mcp.tool()
    def get_sync_status() -> dict:
        """
        获取数据同步队列与失败状态（含 dead-letter 概览）

        Returns:
            dict: {
                "success": bool,
                "metrics": dict,
                "dead_letters": {"count": int, "path": str}
            }

        Examples:
            get_sync_status()
        """
        metrics = data_sync_service.get_sync_metrics()
        dlq = data_sync_service.get_dead_letters(limit=1)
        return _sync_envelope({
            "success": True,
            "metrics": metrics,
            "dead_letters": {
                "count": dlq.get("count", 0),
                "path": dlq.get("path", ""),
            },
        }, source="data_sync.status")

    @mcp.tool()
    def get_dead_letters(limit: int = 20) -> dict:
        """
        获取最近的 dead-letter 失败落盘记录

        Args:
            limit (int, optional): 返回条数上限，默认 20

        Returns:
            dict: {"success": bool, "path": str, "count": int, "records": list}

        Examples:
            get_dead_letters()
            get_dead_letters(limit=50)
        """
        if int(limit or 0) <= 0:
            return _sync_envelope(
                fail("limit 必须为正整数"),
                source="data_sync.dead_letters",
            )
        return _sync_envelope(
            data_sync_service.get_dead_letters(limit=limit),
            source="data_sync.dead_letters",
        )

    @mcp.tool()
    def clear_dead_letters() -> dict:
        """
        清空 dead-letter 失败记录

        Returns:
            dict: {"success": bool, "removed": int, "path": str}

        Examples:
            clear_dead_letters()
        """
        return _sync_envelope(
            data_sync_service.clear_dead_letters(),
            source="data_sync.dead_letters",
        )


    @mcp.tool()
    def get_cache_stats() -> dict:
        """
        获取缓存统计信息

        Returns:
            dict: {"file_count": int, "total_size_mb": float, "hit_rate": float, "cache_dir": str, "ttl_config": dict}

        Examples:
            get_cache_stats()
        """
        stats = cache.get_stats()
        stats["ttl_config"] = CACHE_TTL
        result = ok(stats)
        # 保留平铺字段，兼容历史调用方直接读取 file_count/hit_rate 等键。
        result.update(stats)
        return result

    @mcp.tool()
    def clear_cache() -> dict:
        """
        清空所有缓存

        Returns:
            dict: {"success": bool, "cleared_count": int, "message": str}

        Examples:
            clear_cache()
        """
        count = cache.clear()
        return _sync_envelope({
            "success": True,
            "cleared_count": count,
            "message": f"已清除 {count} 个缓存文件"
        }, source="data_sync.cache")
