"""盘口数据模块"""

import time
import requests
from typing import Optional, Any
from ..market.helpers import (
    normalize_code, parse_numeric, pick_value,
    ok, fail
)
from ...core.cache_manager import cached
from ...core.rate_limiter import get_limiter
from ...data_source import data_source
from ...services.market_data_access import (
    FALLBACK_DB_ONLY,
    attach_quote_access_meta,
    get_quote_snapshot_sync,
)
from ...utils import (
    attach_argument_contract_meta,
    enrich_response_meta,
    propagate_data_quality_to_top,
    resolve_canonical_arg,
    resolve_existing_security_code_sync,
    safe_stderr_print,
    validate_int_range,
)
try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd


def _build_exchange_code(code: str) -> str:
    symbol = normalize_code(code)
    if symbol.startswith(("0", "3")):
        return f"sz{symbol}"
    return f"sh{symbol}"


def _get_order_book_sina_direct(code: str) -> Optional[dict]:
    """Sina 直连：五档盘口"""
    try:
        symbol = normalize_code(code)
        sina_code = _build_exchange_code(symbol)
        url = f"http://hq.sinajs.cn/list={sina_code}"
        headers = {"Referer": "https://finance.sina.com.cn/"}
        resp = requests.get(url, headers=headers, timeout=5)
        text = resp.text
        if "=" not in text or '="' not in text:
            return None

        content = text.split('="', 1)[1].strip('";\n')
        if not content:
            return None

        parts = content.split(",")
        if len(parts) < 30:
            return None

        bids: list[dict] = []
        asks: list[dict] = []
        bid_volume_idx = [10, 12, 14, 16, 18]
        bid_price_idx = [11, 13, 15, 17, 19]
        ask_volume_idx = [20, 22, 24, 26, 28]
        ask_price_idx = [21, 23, 25, 27, 29]

        for i in range(5):
            bid_price = parse_numeric(parts[bid_price_idx[i]]) if bid_price_idx[i] < len(parts) else None
            bid_volume = parse_numeric(parts[bid_volume_idx[i]]) if bid_volume_idx[i] < len(parts) else None
            if bid_price is not None or bid_volume is not None:
                bids.append({"price": bid_price or 0, "volume": int(bid_volume or 0)})

            ask_price = parse_numeric(parts[ask_price_idx[i]]) if ask_price_idx[i] < len(parts) else None
            ask_volume = parse_numeric(parts[ask_volume_idx[i]]) if ask_volume_idx[i] < len(parts) else None
            if ask_price is not None or ask_volume is not None:
                asks.append({"price": ask_price or 0, "volume": int(ask_volume or 0)})

        if not bids and not asks:
            return None

        return {
            "code": symbol,
            "bids": bids,
            "asks": asks,
            "timestamp": int(time.time() * 1000),
        }
    except Exception:
        return None


def _get_order_book_tencent_direct(code: str) -> Optional[dict]:
    """Tencent 直连：五档盘口"""
    try:
        symbol = normalize_code(code)
        qt_code = _build_exchange_code(symbol)
        url = f"http://qt.gtimg.cn/q={qt_code}"
        resp = requests.get(url, timeout=5)
        text = resp.text
        if "=" not in text or '="' not in text:
            return None

        content = text.split('="', 1)[1].strip('";\n')
        if not content:
            return None

        parts = content.split("~")
        if len(parts) < 29:
            return None

        bids: list[dict] = []
        asks: list[dict] = []
        bid_price_idx = [9, 11, 13, 15, 17]
        bid_volume_idx = [10, 12, 14, 16, 18]
        ask_price_idx = [19, 21, 23, 25, 27]
        ask_volume_idx = [20, 22, 24, 26, 28]

        for i in range(5):
            bid_price = parse_numeric(parts[bid_price_idx[i]]) if bid_price_idx[i] < len(parts) else None
            bid_volume = parse_numeric(parts[bid_volume_idx[i]]) if bid_volume_idx[i] < len(parts) else None
            if bid_price is not None or bid_volume is not None:
                bids.append({"price": bid_price or 0, "volume": int(bid_volume or 0)})

            ask_price = parse_numeric(parts[ask_price_idx[i]]) if ask_price_idx[i] < len(parts) else None
            ask_volume = parse_numeric(parts[ask_volume_idx[i]]) if ask_volume_idx[i] < len(parts) else None
            if ask_price is not None or ask_volume is not None:
                asks.append({"price": ask_price or 0, "volume": int(ask_volume or 0)})

        if not bids and not asks:
            return None

        return {
            "code": symbol,
            "bids": bids,
            "asks": asks,
            "timestamp": int(time.time() * 1000),
        }
    except Exception:
        return None


def _ok_order_book(payload: dict, *, access_meta: dict | None = None, source_chain: list[str] | None = None) -> dict:
    response = ok(payload)
    chain = list(source_chain or payload.get("source_chain") or [payload.get("source") or "unknown"])
    response["source_chain"] = chain
    response["backend_requested"] = chain[0] if chain else payload.get("source")
    response["backend_used"] = payload.get("backend_used") or payload.get("source")
    response["fallback_used"] = bool(payload.get("fallback_used"))
    response["fallback_reason"] = payload.get("fallback_reason")
    flags = list(response.get("quality_flags") or [])
    if payload.get("depth_degraded"):
        flags.extend(["partial", "depth_degraded", "degraded"])
        response["degraded"] = True
    if payload.get("stale"):
        flags.extend(["stale", "degraded"])
        response["degraded"] = True
    if access_meta:
        response = attach_quote_access_meta(response, access_meta)
    return propagate_data_quality_to_top(enrich_response_meta(
        response,
        source=payload.get("source") or response.get("source"),
        source_chain=chain,
        quality_flags=flags,
        degraded=bool(response.get("degraded")) or bool(payload.get("depth_degraded")) or bool(payload.get("stale")),
        fallback_used=bool(response.get("fallback_used")),
    ))


def _attach_live_meta(response: dict, *, backend_used: str, source_chain: list[str] | None = None) -> dict:
    chain = list(source_chain or ["live_only_explicit", backend_used])
    response["source_chain"] = chain
    response["backend_requested"] = chain[0] if chain else "live_only_explicit"
    response["backend_used"] = backend_used
    response["fallback_used"] = len(chain) > 1
    response["fallback_reason"] = None
    return response


def _build_degraded_order_book_from_quote(code: str, quote_data: dict, access_meta: dict) -> dict | None:
    price = parse_numeric(quote_data.get("price"))
    if price is None or price <= 0:
        return None
    return {
        "code": code,
        "bids": [{"price": float(price), "volume": 0}],
        "asks": [{"price": float(price), "volume": 0}],
        "timestamp": int(time.time() * 1000),
        "source": "db.stock_quotes",
        "backend_requested": access_meta.get("backend_requested"),
        "backend_used": access_meta.get("backend_used"),
        "fallback_used": access_meta.get("fallback_used"),
        "fallback_reason": access_meta.get("fallback_reason"),
        "db_snapshot_time": access_meta.get("db_snapshot_time"),
        "data_freshness_seconds": access_meta.get("data_freshness_seconds"),
        "stale": access_meta.get("stale"),
        "depth_degraded": True,
        "depth_degraded_reason": "db_snapshot_has_quote_without_level2_depth",
        "source_chain": access_meta.get("source_chain") or ["db.stock_quotes"],
    }


def _parse_order_book_df(df: pd.DataFrame, code: str) -> Optional[dict]:
    if df is None or df.empty:
        return None

    bids: list[dict] = []
    asks: list[dict] = []

    if "item" in df.columns and "value" in df.columns:
        mapping = {str(row.get("item", "")).strip(): row.get("value") for _, row in df.iterrows()}

        def pick_price(keys: list[str]) -> Optional[float]:
            for key in keys:
                if key in mapping:
                    val = parse_numeric(mapping.get(key))
                    if val is not None:
                        return val
            return None

        def pick_volume(keys: list[str]) -> Optional[int]:
            for key in keys:
                if key in mapping:
                    val = parse_numeric(mapping.get(key))
                    if val is not None:
                        return int(val)
            return None

        for i in range(1, 6):
            price = pick_price([f"买{i}", f"买{i}价", f"买{i}价格"])
            volume = pick_volume([f"买{i}量", f"买{i}手", f"买{i}数量"])
            if price is not None or volume is not None:
                bids.append({"price": price or 0, "volume": volume or 0})

            price = pick_price([f"卖{i}", f"卖{i}价", f"卖{i}价格"])
            volume = pick_volume([f"卖{i}量", f"卖{i}手", f"卖{i}数量"])
            if price is not None or volume is not None:
                asks.append({"price": price or 0, "volume": volume or 0})
    else:
        row = df.iloc[0].to_dict()
        for i in range(1, 6):
            bid_price = parse_numeric(pick_value(pd.Series(row), [f"买{i}价", f"买{i}", f"bid{i}"]))
            bid_volume = parse_numeric(pick_value(pd.Series(row), [f"买{i}量", f"buy{i}"]))
            if bid_price is not None or bid_volume is not None:
                bids.append({"price": bid_price or 0, "volume": int(bid_volume or 0)})

            ask_price = parse_numeric(pick_value(pd.Series(row), [f"卖{i}价", f"卖{i}", f"ask{i}"]))
            ask_volume = parse_numeric(pick_value(pd.Series(row), [f"卖{i}量", f"sell{i}"]))
            if ask_price is not None or ask_volume is not None:
                asks.append({"price": ask_price or 0, "volume": int(ask_volume or 0)})

    if not bids and not asks:
        return None

    return {
        "code": code,
        "bids": bids,
        "asks": asks,
        "timestamp": int(time.time() * 1000),
    }


def _parse_trade_direction(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "neutral"
    if "买" in text:
        return "buy"
    if "卖" in text:
        return "sell"
    return "neutral"


def _get_trade_details_tencent_direct(code: str, limit: int) -> Optional[list[dict]]:
    """Tencent 直连：成交明细"""
    try:
        symbol = normalize_code(code)
        qt_code = _build_exchange_code(symbol)
        url = f"http://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={qt_code}&p=1"
        resp = requests.get(url, timeout=5)
        text = resp.text
        if '"' not in text:
            return None

        start = text.find('"')
        end = text.rfind('"')
        if start < 0 or end <= start:
            return None

        content = text[start + 1:end]
        if not content:
            return None

        items = [item for item in content.split("|") if item]
        if not items:
            return None

        details: list[dict] = []
        for item in items[-limit:]:
            parts = item.split("/")
            if len(parts) < 7:
                continue
            time_str = parts[1]
            price = parse_numeric(parts[2]) or 0
            volume = int(parse_numeric(parts[4]) or 0)
            direction_raw = str(parts[6]).strip().upper()
            direction = "neutral"
            if direction_raw == "B":
                direction = "buy"
            elif direction_raw == "S":
                direction = "sell"
            details.append(
                {
                    "time": time_str,
                    "price": price,
                    "volume": volume,
                    "direction": direction,
                }
            )

        return details if details else None
    except Exception:
        return None


@cached(ttl=5.0)
def get_order_book(
    code: str = "",
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
    realtime: bool = False,
    live: bool = False,
) -> dict:
    """获取五档盘口数据

    数据源优先级: AkShare -> Sina -> Tencent

    Args:
        stock_code (str, required): 股票代码，6位数字，如 "600519"、"000001"

    Returns:
        dict: {"success": bool, "data": {...}}
        data 字段:
        - code (str): 股票代码
        - bids (list[dict]): 买盘五档，每项含 price(float) 和 volume(int)
        - asks (list[dict]): 卖盘五档，每项含 price(float) 和 volume(int)
        - timestamp (int): 毫秒级时间戳
        - source (str): 数据来源标识（如数据源提供）

    Errors:
        - 所有数据源均不可用时返回 success=false

    Examples:
        get_order_book("600519")
        get_order_book("000001")
    """
    limiter = get_limiter("quote", max_calls=10, period=1.0)
    limiter.acquire()

    raw_code, alias_hits, _ = resolve_canonical_arg(
        "code",
        code,
        stock_code=stock_code,
        symbol=symbol,
        ticker=ticker,
    )
    code = normalize_code(raw_code)
    explicit_live = bool(realtime or live)
    canonical_args = {"code": code, "realtime": explicit_live}

    def _respond(payload: dict) -> dict:
        return attach_argument_contract_meta(
            payload,
            canonical_tool="get_order_book",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )

    if not code:
        return _respond(fail("需要提供股票代码（支持 code / stock_code / symbol / ticker）"))

    # 0. tqcenter 主路径（客户端在线时直接拿五档）
    access = get_quote_snapshot_sync(code, fallback_mode=FALLBACK_DB_ONLY)
    if access.get("success") and isinstance(access.get("data"), dict):
        degraded = _build_degraded_order_book_from_quote(code, access.get("data") or {}, access)
        if degraded:
            return _respond(_ok_order_book(degraded, access_meta=access, source_chain=access.get("source_chain")))
    if not explicit_live:
        response = fail(access.get("error") or f"stock_quotes snapshot unavailable for {code}; run data_sync_manager type=quote first")
        response["depth_degraded"] = True
        response = attach_quote_access_meta(response, access)
        return _respond(response)

    try:
        quote = data_source.get_realtime_quote(code)
    except Exception as exc:
        safe_stderr_print(f"[order_book] tqcenter quote failed for {code}: {exc}")
        quote = None
    if quote and quote.get("source") in {"tqcenter", "tdx_online"}:
        bids: list[dict] = []
        asks: list[dict] = []
        for i in range(1, 6):
            bp = quote.get(f"bid{i}")
            bv = quote.get(f"bid_vol{i}")
            if bp not in (None, 0) or bv not in (None, 0):
                bids.append({"price": bp or 0.0, "volume": int(bv or 0)})
            ap = quote.get(f"ask{i}")
            av = quote.get(f"ask_vol{i}")
            if ap not in (None, 0) or av not in (None, 0):
                asks.append({"price": ap or 0.0, "volume": int(av or 0)})
        if bids or asks:
            return _respond(_ok_order_book({
                "code": code,
                "bids": bids,
                "asks": asks,
                "timestamp": int(time.time() * 1000),
                "source": quote.get("source"),
                "backend_requested": "live_only_explicit",
                "backend_used": quote.get("source"),
                "source_chain": ["live_only_explicit", str(quote.get("source") or "data_source.realtime_quote")],
            }))

    # 1. Try AkShare (if available)
    df = None
    if ak is not None:
        for func_name, args in (
            ("stock_bid_ask_em", {"symbol": code}),
            ("stock_bid_ask_em", {"code": code}),
            ("stock_bid_ask_sina", {"symbol": code}),
        ):
            func = getattr(ak, func_name, None)
            if not func:
                continue
            try:
                df = func(**args)
            except Exception:
                df = None
            parsed = _parse_order_book_df(df, code)
            if parsed:
                parsed["source"] = f"akshare.{func_name}"
                parsed["backend_requested"] = "live_only_explicit"
                parsed["backend_used"] = parsed["source"]
                parsed["source_chain"] = ["live_only_explicit", parsed["source"]]
                return _respond(_ok_order_book(parsed))

    # 2. Try direct fetch
    for direct_fetch in (_get_order_book_sina_direct, _get_order_book_tencent_direct):
        parsed = direct_fetch(code)
        if parsed:
            source_name = "sina_direct" if direct_fetch is _get_order_book_sina_direct else "tencent_direct"
            parsed["source"] = source_name
            parsed["backend_requested"] = "live_only_explicit"
            parsed["backend_used"] = source_name
            parsed["source_chain"] = ["live_only_explicit", source_name]
            return _respond(_ok_order_book(parsed))

    return _respond(fail(f"未获取到 {code} 的盘口数据 (尝试源: AkShare, Sina, Tencent)"))


@cached(ttl=5.0)
def get_trade_details(
    code: str = "",
    limit: int = 20,
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
) -> dict:
    """获取成交明细

    Args:
        stock_code (str, required): 股票代码，6位数字，如 "600519"
        limit (int, optional): 返回条数，默认 20

    Returns:
        dict: {"success": bool, "data": list[dict] | dict}
        正常返回 data 为列表，每项含:
        - time (str): 成交时间
        - price (float): 成交价
        - volume (int): 成交量
        - direction (str): 买卖方向，"buy"/"sell"/"neutral"
        非交易时段返回 data 为 dict，含 code/trades(空列表)/message/trading_hours

    Errors:
        - 所有数据源均不可用且处于交易时段时返回 success=false

    Examples:
        get_trade_details("600519")
        get_trade_details("000001", limit=50)
    """
    limiter = get_limiter("quote", max_calls=10, period=1.0)
    limiter.acquire()

    raw_code, alias_hits, _ = resolve_canonical_arg(
        "code",
        code,
        stock_code=stock_code,
        symbol=symbol,
        ticker=ticker,
    )
    code, _, error = resolve_existing_security_code_sync(code=raw_code)
    canonical_args = {"code": code or raw_code, "limit": limit}
    if error:
        return attach_argument_contract_meta(
            fail(error),
            canonical_tool="get_trade_details",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )
    limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1)
    canonical_args["limit"] = limit
    if limit_error:
        return attach_argument_contract_meta(
            fail(limit_error),
            canonical_tool="get_trade_details",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )

    def _respond(payload: dict) -> dict:
        return attach_argument_contract_meta(
            payload,
            canonical_tool="get_trade_details",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )

    df = None
    if ak is not None:
        for func_name, args in (
            ("stock_intraday_em", {"symbol": code}),
            ("stock_intraday_sina", {"symbol": code}),
            ("stock_intraday_em", {"code": code}),
        ):
            func = getattr(ak, func_name, None)
            if not func:
                continue
            try:
                df = func(**args)
            except Exception:
                df = None
            if df is not None and not df.empty:
                break

    if df is None or df.empty:
        direct = _get_trade_details_tencent_direct(code, limit)
        if direct:
            return _respond(_attach_live_meta(ok(direct), backend_used="tencent_direct"))

        # 非交易时段友好提示
        from datetime import datetime, time as dt_time
        now_time = datetime.now().time()
        is_trading = (dt_time(9, 30) <= now_time <= dt_time(11, 30) or
                      dt_time(13, 0) <= now_time <= dt_time(15, 0))
        weekday = datetime.now().weekday()
        if not is_trading or weekday >= 5:
            response = ok({
                'code': code, 'trades': [],
                'message': '当前非交易时段，成交明细数据仅在盘中可用',
                'trading_hours': '09:30-11:30, 13:00-15:00 (工作日)'
            })
            return _respond(_attach_live_meta(response, backend_used="none", source_chain=["akshare_intraday", "tencent_direct"]))

        return _respond(fail(f"未获取到 {code} 的成交明细数据 (尝试源: AkShare, Tencent)"))

    df = df.tail(limit)
    records: list[dict] = []
    for _, row in df.iterrows():
        records.append(
            {
                "time": str(pick_value(row, ["时间", "成交时间", "time"]) or ""),
                "price": parse_numeric(pick_value(row, ["成交价", "价格", "price"])) or 0,
                "volume": int(parse_numeric(pick_value(row, ["成交量", "手数", "数量", "volume", "vol"])) or 0),
                "direction": _parse_trade_direction(pick_value(row, ["买卖盘性质", "性质", "direction"])),
            }
        )

    return _respond(_attach_live_meta(ok(records), backend_used="akshare_intraday"))
