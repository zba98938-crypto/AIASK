"""宏观管理器 - 宏观经济数据"""

from typing import Any
import logging
import time

from ...data_source import data_source
from ...storage import get_db
from ...utils import parse_numeric
from ..manager_protocol import (
    normalize_manager_payload,
    fail_with_meta,
    normalize_manager_kwargs,
    ok_with_meta,
)

logger = logging.getLogger(__name__)
def _breadth_from_pct_list(values: list[float]) -> dict:
    up = sum(1 for value in values if value > 0)
    down = sum(1 for value in values if value < 0)
    flat = sum(1 for value in values if value == 0)
    return {
        "advance_count": up,
        "decline_count": down,
        "flat_count": flat,
        "quoted_count": up + down + flat,
    }


def _fallback_market_breadth() -> tuple[dict, float | None, float | None, str | None]:
    try:
        ts_pro = data_source.get_tushare_pro()
        if ts_pro:
            import datetime as _dt
            for days_back in range(7):
                check_date = (_dt.datetime.now() - _dt.timedelta(days=days_back)).strftime("%Y%m%d")
                try:
                    df = ts_pro.daily(trade_date=check_date, fields="ts_code,pct_chg")
                    if df is not None and not df.empty and len(df) >= 100:
                        pct_values = [float(value) for value in df["pct_chg"].tolist() if value is not None]
                        if pct_values:
                            return _breadth_from_pct_list(pct_values), None, None, f"tushare.daily({check_date})"
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("[MacroManager] Tushare breadth fallback failed: %s", exc)

    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty and "涨跌幅" in df.columns:
            pct_values = []
            turnover_total = 0.0
            market_cap_total = 0.0
            for _, row in df.iterrows():
                pct = parse_numeric(row.get("涨跌幅"))
                if pct is not None:
                    pct_values.append(float(pct))
                amount = parse_numeric(row.get("成交额"))
                if amount is not None:
                    turnover_total += float(amount)
                total_mv = parse_numeric(row.get("总市值"))
                if total_mv is not None:
                    market_cap_total += float(total_mv)
            if pct_values:
                breadth = _breadth_from_pct_list(pct_values)
                turnover = round(turnover_total / 1e12, 4) if turnover_total > 0 else None
                market_cap = round(market_cap_total / 1e12, 4) if market_cap_total > 0 else None
                return breadth, turnover, market_cap, "akshare.stock_zh_a_spot_em"
    except Exception as exc:
        logger.warning("[MacroManager] AkShare breadth fallback failed: %s", exc)

    return {}, None, None, None


def _dedupe_chain(values: list[str]) -> list[str]:
    chain = []
    seen = set()
    for value in values:
        label = str(value or "").strip()
        if not label or label in seen:
            continue
        chain.append(label)
        seen.add(label)
    return chain


async def _build_market_overview(db) -> tuple[dict, list[str]]:
    from ..market.quote import get_index_quote

    index_specs = {
        "sh000001": ("000001", "上证指数"),
        "sz399001": ("399001", "深证成指"),
        "sz399006": ("399006", "创业板指"),
    }

    major_indices = {}
    index_changes = []
    for key, (code, fallback_name) in index_specs.items():
        quote = get_index_quote(code)
        data = quote.get("data", {}) if quote.get("success") else {}
        try:
            value = float(data.get("price")) if data.get("price") is not None else None
        except (TypeError, ValueError):
            value = None
        try:
            change_pct = float(data.get("changePercent")) if data.get("changePercent") is not None else None
        except (TypeError, ValueError):
            change_pct = None

        major_indices[key] = {
            "name": str(data.get("name") or fallback_name),
            "value": value,
            "change": change_pct,
            "source": data.get("source") or ("cache" if quote.get("cached") else "spot"),
        }
        if change_pct is not None:
            index_changes.append(change_pct)

    breadth = {
        "advance_count": None,
        "decline_count": None,
        "flat_count": None,
        "quoted_count": None,
    }
    market_cap = None
    turnover = None
    overview_source = "real_time_quote+sqlite"
    source_chain = ["macro_manager", "market.quote.get_index_quote"]
    try:
        async with db.acquire() as conn:
            latest_quotes = await conn.fetchrow(
                """
                WITH latest_day AS (
                    SELECT MAX(date(time)) AS trade_date
                    FROM stock_quotes
                ),
                latest_quotes AS (
                    SELECT code, change_pct, amount, mkt_cap
                    FROM (
                        SELECT code, change_pct, amount, mkt_cap,
                               ROW_NUMBER() OVER (PARTITION BY code ORDER BY time DESC) AS rn
                        FROM stock_quotes
                        WHERE date(time) = (SELECT trade_date FROM latest_day)
                    ) ranked
                    WHERE rn = 1
                )
                SELECT
                    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS advance_count,
                    SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) AS decline_count,
                    SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) AS flat_count,
                    COUNT(*) AS quoted_count,
                    SUM(amount) AS turnover_total,
                    SUM(mkt_cap) AS market_cap_total
                FROM latest_quotes
                """
            )
            if latest_quotes:
                latest_quotes = dict(latest_quotes)
                source_chain.append("db.stock_quotes")
                breadth = {
                    "advance_count": int(latest_quotes.get("advance_count") or 0),
                    "decline_count": int(latest_quotes.get("decline_count") or 0),
                    "flat_count": int(latest_quotes.get("flat_count") or 0),
                    "quoted_count": int(latest_quotes.get("quoted_count") or 0),
                }
                turnover_total = latest_quotes.get("turnover_total")
                market_cap_total = latest_quotes.get("market_cap_total")
                turnover = round(float(turnover_total) / 1e12, 4) if turnover_total else None
                market_cap = round(float(market_cap_total) / 1e12, 4) if market_cap_total else None

            if market_cap is None:
                stock_row = await conn.fetchrow("SELECT SUM(market_cap) AS total_market_cap FROM stocks")
                if stock_row:
                    source_chain.append("db.stocks")
                    stock_row = dict(stock_row)
                    if stock_row.get("total_market_cap"):
                        market_cap = round(float(stock_row.get("total_market_cap")) / 1e12, 4)
    except Exception as exc:
        logger.warning("[MacroManager] market overview DB aggregate failed: %s", exc)

    quoted_count = int(breadth.get("quoted_count") or 0)
    if quoted_count < 100:
        fallback_breadth, fallback_turnover, fallback_market_cap, fallback_source = _fallback_market_breadth()
        fallback_count = int(fallback_breadth.get("quoted_count") or 0)
        if fallback_count >= 100:
            breadth = fallback_breadth
            turnover = fallback_turnover if fallback_turnover is not None else turnover
            market_cap = fallback_market_cap if fallback_market_cap is not None else market_cap
            overview_source = fallback_source or overview_source
            if fallback_source and fallback_source.startswith("tushare.daily"):
                source_chain.append("tushare.daily")
            elif fallback_source == "akshare.stock_zh_a_spot_em":
                source_chain.append("akshare.stock_zh_a_spot_em")

    avg_change = sum(index_changes) / len(index_changes) if index_changes else 0.0
    adv = breadth.get("advance_count") or 0
    dec = breadth.get("decline_count") or 0
    if avg_change >= 0.8 or adv > dec * 1.2:
        sentiment = "bullish"
    elif avg_change <= -0.8 or dec > adv * 1.2:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    return (
        {
            "market_sentiment": sentiment,
            "major_indices": major_indices,
            "market_cap": market_cap,
            "turnover": turnover,
            "breadth": breadth,
            "source": overview_source,
        },
        _dedupe_chain(source_chain),
    )


def register_macro_manager(mcp):
    """注册宏观管理器工具"""
    
    @mcp.tool()
    async def macro_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """宏观管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/get_indicators/market_overview
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - get_indicators: indicators(list[str], optional, 如 ["cpi","pmi","gdp"]), limit(int, optional)
                - market_overview: 无需额外参数

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            macro_manager(action="help", kwargs="{}")
            # 获取CPI指标
            macro_manager(action="get_indicators", kwargs='{"indicators":["cpi"],"limit":12}')
            # 市场概览
            macro_manager(action="market_overview", kwargs="{}")
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_kwargs(kwargs)

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="macro_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="macro_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'get_indicators': '获取宏观指标（支持 indicator/type 或 indicators 列表）',
                        'market_overview': '市场概览',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['macro_manager'])

            elif action == 'get_indicators':
                # P1-2 修复说明：
                # 旧实现在未命中参数时会默认 gdp，且仅支持单指标，容易出现“请求 CPI/PMI 却返回 GDP”的口径错配。
                # 新实现统一支持 indicator/type + indicators(list/逗号串)，并保证响应只包含请求指标结果。

                def _normalize_indicator_list(raw_kwargs: dict) -> list[str]:
                    values = []

                    # 1) 优先读取 indicators（可为 list / 逗号字符串）
                    raw_list = raw_kwargs.get('indicators')
                    if isinstance(raw_list, list):
                        values.extend(raw_list)
                    elif isinstance(raw_list, str) and raw_list.strip():
                        values.extend([x.strip() for x in raw_list.split(',') if x.strip()])

                    # 2) 兼容单值参数
                    if not values:
                        for key in ('indicator', 'type', 'indicator_type', 'name'):
                            val = raw_kwargs.get(key)
                            if val is not None and str(val).strip():
                                values.append(str(val).strip())
                                break

                    # 3) 保持向后兼容：完全未传时默认 gdp
                    if not values:
                        values = ['gdp']

                    # 4) 归一 + 去重
                    normalized = []
                    seen = set()
                    for v in values:
                        item = str(v).strip().lower()
                        if item and item not in seen:
                            normalized.append(item)
                            seen.add(item)
                    return normalized

                requested_indicators = _normalize_indicator_list(kwargs)

                limit_raw = kwargs.get('limit', 5)
                try:
                    limit = int(limit_raw)
                except Exception:
                    limit = 5
                if limit <= 0:
                    limit = 5

                fallback_indicators = {
                    'gdp': {
                        'value': 121.02,
                        'unit': '万亿元',
                        'period': '2025Q4',
                        'yoy_growth': 5.2
                    },
                    'cpi': {
                        'value': 102.5,
                        'unit': '指数',
                        'period': '2026-01',
                        'yoy_growth': 2.5
                    },
                    'pmi': {
                        'value': 50.8,
                        'unit': '指数',
                        'period': '2026-01',
                        'status': 'expansion'
                    },
                    'ppi': {
                        'value': 98.5,
                        'unit': '指数',
                        'period': '2026-01',
                        'yoy_growth': -1.5
                    },
                    'm2': {
                        'value': 310.5,
                        'unit': '万亿元',
                        'period': '2026-01',
                        'yoy_growth': 8.7
                    },
                }

                results_by_indicator = {}
                sources_by_indicator = {}
                source_chain = ['macro_manager']

                for indicator_type in requested_indicators:
                    data = None
                    source = 'none'

                    # 先尝试真实数据源
                    try:
                        from ..macro import get_macro_indicator
                        result = get_macro_indicator(indicator=indicator_type, limit=limit)
                        if result.get('success') and result.get('data'):
                            data = result.get('data')
                            source = result.get('source') or 'macro_indicator'
                            source_chain.append('macro.get_macro_indicator')
                    except Exception as e:
                        logger.warning(f"[MacroManager] get_macro_indicator failed for {indicator_type}: {e}")

                    # 再用本地 fallback（仅当前请求指标）
                    if data is None and indicator_type in fallback_indicators:
                        data = fallback_indicators[indicator_type]
                        source = 'fallback'
                        source_chain.append('macro_manager.fallback_indicators')

                    results_by_indicator[indicator_type] = data
                    sources_by_indicator[indicator_type] = source

                unsupported = [k for k, v in results_by_indicator.items() if v is None]

                # 向后兼容：单指标请求保留 indicator_type + data 结构
                if len(requested_indicators) == 1:
                    indicator_type = requested_indicators[0]
                    payload = {
                        'indicator_type': indicator_type,
                        'data': results_by_indicator[indicator_type],
                        'source': sources_by_indicator[indicator_type],
                        'requested_indicators': requested_indicators,
                    }
                    if unsupported:
                        payload.update({
                            'supported_indicators': list(fallback_indicators.keys()),
                            'message': f'指标 "{indicator_type}" 暂无数据，支持的指标: {", ".join(fallback_indicators.keys())}',
                        })
                    return _ok(payload, source_chain=_dedupe_chain(source_chain))

                # 多指标：返回分指标结果，严格与请求口径一致
                payload = {
                    'requested_indicators': requested_indicators,
                    'data': results_by_indicator,
                    'sources': sources_by_indicator,
                }
                if unsupported:
                    payload.update({
                        'unsupported_indicators': unsupported,
                        'supported_indicators': list(fallback_indicators.keys()),
                        'message': '部分指标暂无数据，请参考 supported_indicators',
                    })
                return _ok(payload, source_chain=_dedupe_chain(source_chain))
            
            elif action == 'market_overview':
                overview, source_chain = await _build_market_overview(db)
                return _ok(overview, source_chain=source_chain)
            
            else:
                return _fail(
                    f'Unknown action: {action}. Supported: help, get_indicators, market_overview',
                    source_chain=['macro_manager'],
                )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name='macro_manager',
                action=action,
                started_at=start_time,
                source_chain=['macro_manager'],
            )
