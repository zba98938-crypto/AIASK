"""
基础数据工具模块

Phase 3 实现 - MCP 服务开发方案

提供量化策略必需的基础数据：
- 交易日历
- 新股/新债申购信息
- 可转债信息
- 股本数据
"""

import time
from typing import Any, List, Optional

from ..data_source import data_source
from ..provider_contracts import attach_provider_contract_meta
from ..utils import resolve_security_code
from .manager_protocol import ERR_INTERNAL, ERR_PARAM, fail_with_meta, ok_with_meta


def _dedupe_chain(items: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in items:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _backend_source_name(value: Any) -> str:
    token = str(value or "").strip()
    if not token or token == "none":
        return ""
    return f"market_data.{token}"


def _source_chain_from_result(default_source: str, result: dict[str, Any] | None = None) -> list[str]:
    chain = [default_source]
    if not isinstance(result, dict):
        return chain
    for candidate in (result.get("backend_requested"), result.get("backend_used"), result.get("source")):
        source_name = _backend_source_name(candidate)
        if source_name:
            chain.append(source_name)
    return _dedupe_chain(chain)


def _provider_chain_from_result(result: dict[str, Any] | None, default_source: str) -> list[str]:
    if not isinstance(result, dict):
        return [default_source]
    chain = [
        _backend_source_name(candidate)
        for candidate in (result.get("backend_requested"), result.get("backend_used"), result.get("source"))
    ]
    return _dedupe_chain(chain) or [default_source]


def _attach_trading_calendar_contract_meta(
    response: dict[str, Any],
    *,
    raw_result: dict[str, Any] | None = None,
    source_chain: list[str] | None = None,
    data_timestamp: str | None = None,
) -> dict[str, Any]:
    provider_chain = _provider_chain_from_result(raw_result, "data_source.get_trading_dates")
    provider_used = ""
    if isinstance(raw_result, dict):
        provider_used = _backend_source_name(raw_result.get("backend_used") or raw_result.get("source"))
    if not provider_used:
        provider_used = "none" if not response.get("success") else provider_chain[-1]
    quality = {}
    meta = response.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("quality"), dict):
        quality = dict(meta.get("quality") or {})
    return attach_provider_contract_meta(
        response,
        tool_name="get_trading_dates",
        standard_model="TradingCalendar",
        provider_requested=provider_chain[0] if provider_chain else "data_source.get_trading_dates",
        provider_used=provider_used,
        source_chain=provider_chain or source_chain or ["data_source.get_trading_dates"],
        data_timestamp=data_timestamp,
        freshness={
            "expectation": "static_calendar_snapshot_with_period_filter",
            "data_timestamp_field": "dates[-1]",
        },
        quality=quality,
    )


def _read_only_extra_meta(
    *,
    status: str,
    target: str,
    result: dict[str, Any] | None = None,
    degraded: bool | None = None,
    extra_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(result or {})
    quality_flags = [str(item) for item in payload.get("quality_flags") or [] if str(item).strip()]
    fallback_reason = payload.get("fallback_reason")
    fallback_used = bool(payload.get("fallback_used", False))
    quality = {
        "status": status,
        "fallback_used": fallback_used,
    }
    if quality_flags:
        quality["quality_flags"] = quality_flags
    if payload.get("backend_requested") is not None:
        quality["backend_requested"] = payload.get("backend_requested")
    if payload.get("backend_used") is not None:
        quality["backend_used"] = payload.get("backend_used")
    if fallback_reason:
        quality["fallback_reason"] = fallback_reason
    if isinstance(extra_quality, dict):
        quality.update(extra_quality)
    return {
        "quality": quality,
        "side_effect": {
            "level": "read_only",
            "target": target,
            "confirmation_required": False,
            "idempotent": True,
        },
        "degraded": fallback_used if degraded is None else degraded,
    }


def register(mcp):
    """注册基础数据工具"""

    @mcp.tool()
    async def get_trading_dates(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        count: int = -1
    ):
        """
        获取交易日历
        
        Args:
            start_date (str, optional): 起始日期，格式 YYYYMMDD（如 "20260101"）
            end_date (str, optional): 结束日期，格式 YYYYMMDD（如 "20261231"）
            count (int, optional): 返回最近的 count 个交易日，-1 表示全部（默认 -1）
        
        Returns:
            dict: {"success": bool, "data": {"dates": list[str], "count": int, "source": str}}
            dates 格式: ["YYYYMMDD", ...]，按时间升序排列
        
        Errors:
            - start_date/end_date 格式错误时返回失败
            - 数据源不可用时返回失败

        Example:
            get_trading_dates(count=10)
            get_trading_dates(start_date="20260101", end_date="20260131")
        """
        started_at = time.perf_counter()
        source_name = "basic_data.get_trading_dates"
        target = end_date or start_date or "trading_calendar"
        try:
            import datetime as _dt

            def _valid_yyyymmdd(v: Optional[str]) -> bool:
                if not v:
                    return True
                if len(v) != 8 or not v.isdigit():
                    return False
                try:
                    _dt.datetime.strptime(v, "%Y%m%d")
                    return True
                except ValueError:
                    return False

            if not _valid_yyyymmdd(start_date):
                return _attach_trading_calendar_contract_meta(
                    fail_with_meta(
                        "start_date 格式错误，应为 YYYYMMDD",
                        tool_name="get_trading_dates",
                        action="query",
                        started_at=started_at,
                        source_chain=[source_name],
                        error_code=ERR_PARAM,
                        extra_meta=_read_only_extra_meta(
                            status="invalid_request",
                            target=target,
                            degraded=True,
                        ),
                    ),
                    source_chain=[source_name],
                )
            if not _valid_yyyymmdd(end_date):
                return _attach_trading_calendar_contract_meta(
                    fail_with_meta(
                        "end_date 格式错误，应为 YYYYMMDD",
                        tool_name="get_trading_dates",
                        action="query",
                        started_at=started_at,
                        source_chain=[source_name],
                        error_code=ERR_PARAM,
                        extra_meta=_read_only_extra_meta(
                            status="invalid_request",
                            target=target,
                            degraded=True,
                        ),
                    ),
                    source_chain=[source_name],
                )
            if start_date and end_date and start_date > end_date:
                return _attach_trading_calendar_contract_meta(
                    fail_with_meta(
                        "start_date 不能晚于 end_date",
                        tool_name="get_trading_dates",
                        action="query",
                        started_at=started_at,
                        source_chain=[source_name],
                        error_code=ERR_PARAM,
                        extra_meta=_read_only_extra_meta(
                            status="invalid_request",
                            target=target,
                            degraded=True,
                        ),
                    ),
                    source_chain=[source_name],
                )
            if count == 0 or count < -1:
                return _attach_trading_calendar_contract_meta(
                    fail_with_meta(
                        "count 仅支持 -1 或正整数",
                        tool_name="get_trading_dates",
                        action="query",
                        started_at=started_at,
                        source_chain=[source_name],
                        error_code=ERR_PARAM,
                        extra_meta=_read_only_extra_meta(
                            status="invalid_request",
                            target=target,
                            degraded=True,
                        ),
                    ),
                    source_chain=[source_name],
                )

            result = data_source.get_trading_dates(
                market="SH",
                start_time=start_date or "",
                end_time=end_date or "",
                count=count
            )
            source_chain = _source_chain_from_result(source_name, result)

            if result.get("success"):
                payload = {
                    "dates": result["data"],
                    "count": len(result["data"]),
                    "source": result.get("source", "unknown"),
                }
                return _attach_trading_calendar_contract_meta(
                    ok_with_meta(
                        payload,
                        tool_name="get_trading_dates",
                        action="query",
                        started_at=started_at,
                        source_chain=source_chain,
                        extra_meta=_read_only_extra_meta(
                            status="available",
                            target=target,
                            result=result,
                            extra_quality={"result_count": len(result["data"])},
                        ),
                    ),
                    raw_result=result,
                    source_chain=source_chain,
                    data_timestamp=str(result["data"][-1]) if result["data"] else None,
                )
            return _attach_trading_calendar_contract_meta(
                fail_with_meta(
                    result.get("message", "获取交易日历失败"),
                    tool_name="get_trading_dates",
                    action="query",
                    started_at=started_at,
                    source_chain=source_chain,
                    error_code=ERR_INTERNAL,
                    extra_meta=_read_only_extra_meta(
                        status="failed",
                        target=target,
                        result=result,
                        degraded=True,
                    ),
                ),
                raw_result=result,
                source_chain=source_chain,
            )
        except Exception as e:
            return _attach_trading_calendar_contract_meta(
                fail_with_meta(
                    str(e),
                    tool_name="get_trading_dates",
                    action="query",
                    started_at=started_at,
                    source_chain=[source_name],
                    error_code=ERR_INTERNAL,
                    extra_meta=_read_only_extra_meta(
                        status="failed",
                        target=target,
                        degraded=True,
                    ),
                ),
                source_chain=[source_name],
            )

    @mcp.tool()
    async def get_ipo_info(
        ipo_type: int = 2,
        include_future: bool = True
    ):
        """
        获取新股/新债申购信息
        
        Args:
            ipo_type (int, optional): 申购类型，默认 2
                - 0: 仅新股申购
                - 1: 仅新发债
                - 2: 新股和新发债
            include_future (bool, optional): 是否包含未来申购信息，默认 True
        
        Returns:
            dict: {"success": bool, "data": {"ipo_list": list[dict], "count": int, "source": str}}
            每条记录包含: 代码、名称、申购日期、发行价格等（字段因数据源而异）

        Errors:
            - 当前无申购信息时返回空列表（success=true, count=0）

        Example:
            get_ipo_info()
            get_ipo_info(ipo_type=0, include_future=False)
        """
        started_at = time.perf_counter()
        source_name = "basic_data.get_ipo_info"
        target = f"ipo_type:{ipo_type}"
        try:
            ipo_date = 1 if include_future else 0
            result = data_source.get_ipo_info(ipo_type=ipo_type, ipo_date=ipo_date)
            source_chain = _source_chain_from_result(source_name, result)
            
            if result.get("success") and result.get("data"):
                payload = {
                    "ipo_list": result["data"],
                    "count": len(result["data"]),
                    "source": result.get("source", "unknown"),
                }
                return ok_with_meta(
                    payload,
                    tool_name="get_ipo_info",
                    action="query",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_read_only_extra_meta(
                        status="available",
                        target=target,
                        result=result,
                        extra_quality={"result_count": len(result["data"])},
                    ),
                )
            
            # 返回空但成功的结果（而非 fail），避免链路中断
            return ok_with_meta({
                "ipo_list": [],
                "count": 0,
                "source": "none",
                "message": "当前暂无新股/新债申购信息"
            },
                tool_name="get_ipo_info",
                action="query",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_read_only_extra_meta(
                    status="empty",
                    target=target,
                    result=result,
                    extra_quality={"result_count": 0},
                ),
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="get_ipo_info",
                action="query",
                started_at=started_at,
                source_chain=[source_name],
                error_code=ERR_INTERNAL,
                extra_meta=_read_only_extra_meta(
                    status="failed",
                    target=target,
                    degraded=True,
                ),
            )

    @mcp.tool()
    async def get_cb_info(
        code: Optional[str] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
    ):
        """
        获取可转债基础信息
        
        Args:
            code (str, required): 可转债代码，如 "123039" 或 "123039.SZ"
        
        Returns:
            dict: {"success": bool, "data": {"cb_info": dict, "source": str}}
            cb_info 包含:
            - KZZCode(str): 可转债代码
            - HSCode(str): 正股代码
            - ZGPrice(float): 转股价格
            - ZGDate(str): 转股日期
            - EndDate(str): 到期日期
            - RestScope(float): 剩余规模

        Errors:
            - 代码为空时返回错误
            - 代码不存在时返回失败

        Example:
            get_cb_info("123039")
        """
        started_at = time.perf_counter()
        source_name = "basic_data.get_cb_info"
        try:
            code = resolve_security_code(code, stock_code=stock_code, symbol=symbol)
            if not code:
                return fail_with_meta(
                    "可转债代码不能为空（支持 code / stock_code / symbol）",
                    tool_name="get_cb_info",
                    action="query",
                    started_at=started_at,
                    source_chain=[source_name],
                    error_code=ERR_PARAM,
                    extra_meta=_read_only_extra_meta(
                        status="invalid_request",
                        target="convertible_bond",
                        degraded=True,
                    ),
                )
            
            result = data_source.get_cb_info(stock_code=code)
            source_chain = _source_chain_from_result(source_name, result)
            
            if result.get("success"):
                payload = {
                    "cb_info": result["data"],
                    "source": result.get("source", "unknown"),
                }
                return ok_with_meta(
                    payload,
                    tool_name="get_cb_info",
                    action="query",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_read_only_extra_meta(
                        status="available",
                        target=code,
                        result=result,
                        ),
                    )
            return ok_with_meta(
                {
                    "cb_info": result.get("data") or {},
                    "source": result.get("source") or result.get("backend_used") or "none",
                    "message": result.get("message", "未获取到可转债信息，已降级为空结果"),
                    "degraded": True,
                    "fallback_used": True,
                    "fallback_reason": result.get("fallback_reason") or result.get("message"),
                },
                tool_name="get_cb_info",
                action="query",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_read_only_extra_meta(
                    status="empty",
                    target=code,
                    result=result,
                    degraded=True,
                    extra_quality={"result_count": 0},
                ),
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="get_cb_info",
                action="query",
                started_at=started_at,
                source_chain=[source_name],
                error_code=ERR_INTERNAL,
                extra_meta=_read_only_extra_meta(
                    status="failed",
                    target=code or "convertible_bond",
                    degraded=True,
                ),
            )

    @mcp.tool()
    async def get_stock_capital(
        code: Optional[str] = None,
        dates: Optional[List[str]] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
    ):
        """
        获取股票股本数据
        
        Args:
            code (str, required): 股票代码，如 "600519" 或 "600519.SH"
            dates (list[str], optional): 日期列表，格式 ["YYYYMMDD", ...]，须从小到大排序；
                                         不提供则返回最新数据
        
        Returns:
            dict: {"success": bool, "data": {"capital_data": list[dict], "count": int, "source": str}}
            每条记录包含:
            - Date(str): 日期
            - ltgb(int): 流通股本（股）
            - zgb(int): 总股本（股）

        Errors:
            - 代码为空时返回错误
            - 数据源不可用时返回失败

        Example:
            get_stock_capital("600519")
            get_stock_capital("600519", dates=["20260101", "20260601"])
        """
        started_at = time.perf_counter()
        source_name = "basic_data.get_stock_capital"
        try:
            code = resolve_security_code(code, stock_code=stock_code, symbol=symbol, ticker=ticker)
            if not code:
                return fail_with_meta(
                    "股票代码不能为空（支持 code / stock_code / symbol / ticker）",
                    tool_name="get_stock_capital",
                    action="query",
                    started_at=started_at,
                    source_chain=[source_name],
                    error_code=ERR_PARAM,
                    extra_meta=_read_only_extra_meta(
                        status="invalid_request",
                        target="stock_capital",
                        degraded=True,
                    ),
                )
            
            date_list = dates or []
            count = len(date_list) if date_list else 1
            
            result = data_source.get_gb_info(
                stock_code=code,
                date_list=date_list,
                count=count
            )
            source_chain = _source_chain_from_result(source_name, result)
            
            if result.get("success"):
                capital_data = result.get("data") or []
                payload = {
                    "capital_data": capital_data,
                    "count": len(capital_data),
                    "source": result.get("source", "unknown"),
                    "message": result.get("message"),
                    "backend_requested": result.get("backend_requested"),
                    "backend_used": result.get("backend_used"),
                    "fallback_used": result.get("fallback_used", False),
                    "fallback_reason": result.get("fallback_reason"),
                    "quality_flags": result.get("quality_flags", []),
                    "asof_time": result.get("asof_time"),
                    "freshness_sec": result.get("freshness_sec"),
                    "latency_ms": result.get("latency_ms"),
                }
                return ok_with_meta(
                    payload,
                    tool_name="get_stock_capital",
                    action="query",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_read_only_extra_meta(
                        status="available",
                        target=code,
                        result=result,
                        extra_quality={"result_count": len(capital_data)},
                    ),
                )

            # 降级: 从 get_stock_info 提取股本数据
            from .finance import get_stock_info
            info_result = get_stock_info(code)
            if info_result.get("success") and info_result.get("data"):
                info_data = info_result["data"]
                total_shares = info_data.get("totalShares")
                float_shares = info_data.get("floatShares")
                if total_shares or float_shares:
                    import datetime
                    today = datetime.datetime.now().strftime('%Y%m%d')
                    from ..utils import parse_numeric
                    capital_entry = {
                        "Date": int(today),
                        "zgb": float(parse_numeric(total_shares) or 0),
                        "ltgb": float(parse_numeric(float_shares) or 0),
                    }
                    payload = {
                        "capital_data": [capital_entry],
                        "count": 1,
                        "source": "get_stock_info_fallback",
                        "message": "通过 get_stock_info 降级获取股本数据",
                        "fallback_used": True,
                        "fallback_reason": result.get("message") or "get_gb_info failed",
                    }
                    return ok_with_meta(
                        payload,
                        tool_name="get_stock_capital",
                        action="query",
                        started_at=started_at,
                        source_chain=_dedupe_chain(source_chain + ["finance.get_stock_info"]),
                        extra_meta=_read_only_extra_meta(
                            status="available",
                            target=code,
                            result={
                                **result,
                                "fallback_used": True,
                                "fallback_reason": result.get("message") or "get_gb_info failed",
                                "backend_used": "get_stock_info_fallback",
                            },
                            degraded=True,
                            extra_quality={"result_count": 1},
                        ),
                    )

            fallback_reason = (
                result.get("fallback_reason")
                or result.get("message")
                or result.get("error")
                or "provider_unavailable"
            )
            quality_flags = []
            for flag in list(result.get("quality_flags") or []) + [
                "empty",
                "degraded",
                "provider_unavailable",
            ]:
                text = str(flag or "").strip()
                if text and text not in quality_flags:
                    quality_flags.append(text)
            degraded_result = {
                **result,
                "fallback_used": True,
                "fallback_reason": fallback_reason,
                "quality_flags": quality_flags,
                "backend_used": result.get("backend_used") or result.get("source") or "none",
            }
            return ok_with_meta(
                {
                    "capital_data": [],
                    "count": 0,
                    "source": degraded_result.get("source") or degraded_result.get("backend_used") or "none",
                    "message": result.get("message") or "stock capital provider unavailable; returned degraded empty result",
                    "backend_requested": result.get("backend_requested"),
                    "backend_used": degraded_result.get("backend_used"),
                    "fallback_used": True,
                    "fallback_reason": fallback_reason,
                    "quality_flags": quality_flags,
                    "degraded": True,
                },
                tool_name="get_stock_capital",
                action="query",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_read_only_extra_meta(
                    status="empty",
                    target=code,
                    result=degraded_result,
                    degraded=True,
                    extra_quality={
                        "result_count": 0,
                        "quality_flags": quality_flags,
                    },
                ),
            )

            return fail_with_meta(
                result.get("message") or result.get("error") or "获取股本数据失败",
                tool_name="get_stock_capital",
                action="query",
                started_at=started_at,
                source_chain=source_chain,
                error_code=ERR_INTERNAL,
                extra_meta=_read_only_extra_meta(
                    status="failed",
                    target=code,
                    result=result,
                    degraded=True,
                ),
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="get_stock_capital",
                action="query",
                started_at=started_at,
                source_chain=[source_name],
                error_code=ERR_INTERNAL,
                extra_meta=_read_only_extra_meta(
                    status="failed",
                    target=code or "stock_capital",
                    degraded=True,
                ),
            )
