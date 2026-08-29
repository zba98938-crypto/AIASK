from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional

try:
    import akshare as ak
except ImportError:
    ak = None

from ..core.cache_manager import cached
from ..core.rate_limiter import get_limiter
from ..provider_contracts import attach_tool_provider_contract_meta
from ..utils import (
    fail,
    ok,
    parse_numeric,
    safe_int,
)

# 最近一次可用的到期月份缓存（按标的维度）
_LAST_KNOWN_MONTHS: dict[str, list[str]] = {}


def _with_provider_contract(result: dict, **kwargs: Any) -> dict:
    return attach_tool_provider_contract_meta(
        result,
        tool_name="get_option_chain",
        standard_model="OptionChain",
        **kwargs,
    )


def _empty_degraded_option_chain(
    *,
    symbol: str,
    underlying_code: str,
    underlying_spot: str,
    source_chain: list[str],
    fallback_reason: list[str] | str,
) -> dict:
    reasons = fallback_reason if isinstance(fallback_reason, list) else [str(fallback_reason)]
    return _with_provider_contract(
        ok(
            {
                "underlying": {"code": underlying_code, "symbol": underlying_spot, "name": symbol},
                "expiryMonths": [],
                "selectedExpiry": [],
                "options": [],
                "truncated": False,
                "source_chain": source_chain,
                "fallback_reason": reasons,
                "degraded": True,
            }
        ),
        provider_used=source_chain[-1] if source_chain else "none",
        source_chain=source_chain,
        fallback_reason=reasons,
    )


def _default_recent_months(n: int = 3) -> list[str]:
    """生成默认最近 n 个月（YYYYMM）。"""
    now = datetime.now()
    months: list[str] = []
    year, month = now.year, now.month
    for i in range(max(1, n)):
        m = month + i
        y = year + (m - 1) // 12
        mm = (m - 1) % 12 + 1
        months.append(f"{y:04d}{mm:02d}")
    return months


@cached(ttl=5.0)  # 5秒缓存，期权数据实时性要求高
def get_option_chain(underlying: str, expiry_month: str = "", limit: int = 200) -> dict:
    """
    获取上交所ETF期权链数据（Sina）

    已知限制与降级策略：
    - 当上游月份列表返回 None/空值/异常结构时，不抛异常；按“历史缓存月份 -> 默认最近3个月”降级。
    - 当指定月份不在可用列表中时，仍尝试按指定月份拉取合约（兼容部分上游延迟场景）。
    - 当部分月份或合约拉取失败时，跳过失败项并继续返回可用结果。
    - 上游不可用时依然返回 success=true（options 可为空），并通过 fallback_reason/degraded 说明降级。
    - 返回中新增 source_chain / fallback_reason / degraded 便于审计，不影响原有字段兼容性。

    Args:
        underlying: 标的代码 510050/510300 或 50ETF/300ETF
        expiry_month: 到期月份 YYYY-MM 或 YYYYMM，不传默认最近到期
        limit: 最大合约数量（默认200）
    """
    limiter = get_limiter("options", rate=5.0)  # 5次/秒
    limiter.acquire()

    source_chain: list[str] = []
    fallback_reason: list[str] = []

    def _normalize_month_token(value: Any) -> str:
        text = str(value or "").strip().replace("-", "")
        return text if len(text) == 6 and text.isdigit() else ""

    def _normalize_months(raw_months: Any) -> list[str]:
        if raw_months is None:
            return []
        values: list[Any] = []
        if isinstance(raw_months, (list, tuple, set)):
            values = list(raw_months)
        elif hasattr(raw_months, "tolist"):
            try:
                values = list(raw_months.tolist())
            except Exception:
                values = []
        elif hasattr(raw_months, "empty") and hasattr(raw_months, "columns"):
            try:
                if not raw_months.empty:
                    col = "month" if "month" in raw_months.columns else raw_months.columns[0]
                    values = list(raw_months[col].tolist())
            except Exception:
                values = []
        else:
            values = [raw_months]

        normalized: list[str] = []
        for item in values:
            token = _normalize_month_token(item)
            if token and token not in normalized:
                normalized.append(token)
        return normalized

    try:
        raw_underlying = str(underlying or "").strip().upper()
        underlying_map = {
            "510050": "50ETF",
            "50ETF": "50ETF",
            "510300": "300ETF",
            "300ETF": "300ETF",
        }
        symbol = underlying_map.get(raw_underlying)
        if not symbol:
            return _with_provider_contract(
                fail(f"不支持的标的: {underlying}"),
                provider_used="none",
                fallback_reason=f"unsupported underlying: {underlying}",
            )

        source_chain.append("akshare.option_sse_list_sina")
        underlying_code = "510050" if symbol == "50ETF" else "510300"
        underlying_spot = f"sh{underlying_code}"

        degraded = False
        required_funcs = (
            "option_sse_list_sina",
            "option_sse_codes_sina",
            "option_sse_spot_price_sina",
            "option_sse_underlying_spot_price_sina",
        )
        missing_funcs = [name for name in required_funcs if ak is None or not hasattr(ak, name)]
        if missing_funcs:
            return _empty_degraded_option_chain(
                symbol=symbol,
                underlying_code=underlying_code,
                underlying_spot=underlying_spot,
                source_chain=["akshare.option_sse"],
                fallback_reason=[f"akshare options provider unavailable: {', '.join(missing_funcs)}"],
            )

        try:
            raw_months = ak.option_sse_list_sina(symbol=symbol)
            months = _normalize_months(raw_months)
            if months:
                _LAST_KNOWN_MONTHS[symbol] = months[:]
            else:
                cached_months = _LAST_KNOWN_MONTHS.get(symbol, [])
                if cached_months:
                    months = cached_months[:]
                    degraded = True
                    fallback_reason.append("上游到期月份为空，已回退到历史缓存月份")
                else:
                    months = _default_recent_months(3)
                    degraded = True
                    fallback_reason.append("上游到期月份为空，已回退到默认最近3个月")
        except Exception as e:
            cached_months = _LAST_KNOWN_MONTHS.get(symbol, [])
            if cached_months:
                months = cached_months[:]
                degraded = True
                fallback_reason.append(f"到期月份列表拉取失败({e})，已回退到历史缓存月份")
            else:
                months = _default_recent_months(3)
                degraded = True
                fallback_reason.append(f"到期月份列表拉取失败({e})，已回退到默认最近3个月")

        raw_month = _normalize_month_token(expiry_month)
        if str(expiry_month or "").strip() and not raw_month:
            return _with_provider_contract(
                fail("expiry_month 格式错误，应为 YYYY-MM 或 YYYYMM"),
                provider_used="none",
                source_chain=source_chain,
                fallback_reason="invalid expiry_month",
            )

        if raw_month:
            valid_months = [raw_month]
            if raw_month not in months:
                degraded = True
                fallback_reason.append(f"指定月份 {raw_month} 不在上游列表中，已尝试按指定月份拉取")
        else:
            valid_months = [months[0]] if months else []

        if not valid_months:
            degraded = True
            fallback_reason.append("无可用到期月份，返回空结果")

        limit = int(limit)
        if limit <= 0:
            limit = 200
        limit = min(limit, 1000)

        contracts: list[dict[str, Any]] = []
        source_chain.append("akshare.option_sse_codes_sina")
        for month in valid_months:
            try:
                call_df = ak.option_sse_codes_sina(symbol="看涨期权", trade_date=month, underlying=underlying_code)
                put_df = ak.option_sse_codes_sina(symbol="看跌期权", trade_date=month, underlying=underlying_code)
            except Exception as e:
                fallback_reason.append(f"{month} 合约列表拉取失败: {e}")
                continue

            call_codes = call_df["期权代码"].dropna().astype(str).tolist() if call_df is not None and "期权代码" in getattr(call_df, "columns", []) else []
            put_codes = put_df["期权代码"].dropna().astype(str).tolist() if put_df is not None and "期权代码" in getattr(put_df, "columns", []) else []
            for code in call_codes:
                contracts.append({"code": code, "type": "call", "expiryMonth": month})
            for code in put_codes:
                contracts.append({"code": code, "type": "put", "expiryMonth": month})

        if not contracts:
            degraded = True
            fallback_reason.append("合约列表为空，返回空 options 结果")

        truncated = len(contracts) > limit
        if truncated:
            contracts = contracts[:limit]

        def fetch_contract(contract: dict) -> Optional[dict]:
            code = contract["code"]
            df = ak.option_sse_spot_price_sina(symbol=code)
            if df is None or getattr(df, "empty", True) or "字段" not in getattr(df, "columns", []):
                return None
            data = dict(zip(df["字段"], df["值"]))
            return {
                "code": code,
                "name": str(data.get("期权合约简称", "")),
                "type": contract["type"],
                "expiryMonth": contract["expiryMonth"],
                "strike": parse_numeric(data.get("行权价")),
                "last": parse_numeric(data.get("最新价")),
                "bid": parse_numeric(data.get("买价")),
                "ask": parse_numeric(data.get("卖价")),
                "bidVolume": safe_int(data.get("买量")),
                "askVolume": safe_int(data.get("卖量")),
                "open": parse_numeric(data.get("开盘价")),
                "high": parse_numeric(data.get("最高价")),
                "low": parse_numeric(data.get("最低价")),
                "prevClose": parse_numeric(data.get("昨收价")),
                "changePercent": parse_numeric(data.get("涨幅")),
                "volume": safe_int(data.get("成交量")),
                "amount": parse_numeric(data.get("成交额")),
                "openInterest": safe_int(data.get("持仓量")),
                "time": str(data.get("行情时间", "")),
                "underlying": str(data.get("标的股票", underlying_code)),
            }

        options: list[dict[str, Any]] = []
        source_chain.append("akshare.option_sse_spot_price_sina")
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(fetch_contract, c) for c in contracts]
            for future in futures:
                try:
                    item = future.result(timeout=10)
                    if item:
                        options.append(item)
                except Exception as e:
                    fallback_reason.append(f"合约行情拉取失败: {e}")

        underlying_info: dict[str, Any] = {"code": underlying_code, "symbol": underlying_spot, "name": symbol}
        source_chain.append("akshare.option_sse_underlying_spot_price_sina")
        try:
            underlying_df = ak.option_sse_underlying_spot_price_sina(symbol=underlying_spot)
        except Exception as e:
            degraded = True
            underlying_df = None
            fallback_reason.append(f"标的行情拉取失败: {e}")
        if underlying_df is not None and not getattr(underlying_df, "empty", True) and "字段" in getattr(underlying_df, "columns", []):
            data = dict(zip(underlying_df["字段"], underlying_df["值"]))
            underlying_info.update(
                {
                    "price": parse_numeric(data.get("最近成交价")),
                    "open": parse_numeric(data.get("今日开盘价")),
                    "preClose": parse_numeric(data.get("昨日收盘价")),
                    "high": parse_numeric(data.get("最高成交价")),
                    "low": parse_numeric(data.get("最低成交价")),
                    "time": str(data.get("行情时间", "")),
                    "date": str(data.get("行情日期", "")),
                }
            )

        if not options and contracts:
            degraded = True
            fallback_reason.append("合约行情为空，返回空 options 结果")

        return _with_provider_contract(
            ok(
                {
                    "underlying": underlying_info,
                    "expiryMonths": months,
                    "selectedExpiry": valid_months,
                    "options": options,
                    "truncated": truncated,
                    "source_chain": source_chain,
                    "fallback_reason": fallback_reason,
                    "degraded": degraded,
                }
            ),
            provider_used=source_chain[-1] if source_chain else "akshare.option_sse",
            source_chain=source_chain,
            fallback_reason=fallback_reason or None,
            data_timestamp=str(underlying_info.get("date") or underlying_info.get("time") or "") or None,
        )
    except Exception as e:
        return _with_provider_contract(
            fail(f"get_option_chain 处理异常: {e}"),
            provider_used="none",
            source_chain=source_chain,
            fallback_reason=str(e),
        )


def register(mcp):
    mcp.tool()(get_option_chain)
