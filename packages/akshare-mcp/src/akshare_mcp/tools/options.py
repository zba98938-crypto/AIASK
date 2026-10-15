from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional


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
    """上交所 ETF 期权链。

    This read akshare's Sina option endpoints. akshare is not a dependency of this
    project and is not installed, so the chain cannot be built on this host. The
    tool still answers ``success=true`` with an empty chain and the reason, which
    is the same contract it already used when the provider was missing.

    Args:
        underlying: 标的代码 510050/510300 或 50ETF/300ETF
        expiry_month: 保留参数；当前无 provider 可用
        limit: 保留参数；当前无 provider 可用
    """
    limiter = get_limiter("options", rate=5.0)  # 5次/秒
    limiter.acquire()

    raw_underlying = str(underlying or "").strip().upper()
    symbol = {
        "510050": "50ETF",
        "50ETF": "50ETF",
        "510300": "300ETF",
        "300ETF": "300ETF",
    }.get(raw_underlying)
    if not symbol:
        return _with_provider_contract(
            fail(f"不支持的标的: {underlying}"),
            provider_used="none",
            fallback_reason=f"unsupported underlying: {underlying}",
        )

    underlying_code = "510050" if symbol == "50ETF" else "510300"
    return _empty_degraded_option_chain(
        symbol=symbol,
        underlying_code=underlying_code,
        underlying_spot=f"sh{underlying_code}",
        # The provider that used to be listed here (akshare's Sina option
        # endpoints) was removed with the dependency; naming it would claim a
        # source that no longer exists.
        source_chain=[],
        fallback_reason=[
            "akshare options provider unavailable: akshare is not a dependency of this project"
        ],
    )


def register(mcp):
    mcp.tool()(get_option_chain)
