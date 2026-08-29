"""交易数据管理器 - 龙虎榜、大单追踪"""

from typing import Any
from datetime import datetime
import logging
import time
from ...storage import get_db
from ...utils import normalize_code
from ..manager_protocol import (
    normalize_manager_payload,
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    ok_with_meta,
)

logger = logging.getLogger(__name__)

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


def register_trading_data_manager(mcp):
    """注册交易数据管理器工具"""

    @mcp.tool()
    async def trading_data_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """交易数据管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/dragon_tiger/block_trades/institutional_flow
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - dragon_tiger: date(str, optional, "YYYY-MM-DD"), stock_code(str, optional)
                - block_trades: date(str, optional), stock_code(str, optional), limit(int, optional)
                - institutional_flow: code(str, optional)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            trading_data_manager(action="help", kwargs="{}")
            # 查询龙虎榜
            trading_data_manager(action="dragon_tiger", kwargs='{"date":"2025-01-15"}')
            # 查询大宗交易
            trading_data_manager(action="block_trades", kwargs='{"stock_code":"600519","limit":10}')
            # 机构资金流向
            trading_data_manager(action="institutional_flow", kwargs="{}")
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_kwargs(kwargs)
            code, kwargs = normalize_manager_code(None, kwargs)
            if code:
                kwargs['code'] = normalize_code(code)

            def _ok(data: dict, source_chain=None, data_timestamp: str | None = None):
                return ok_with_meta(
                    data,
                    tool_name="trading_data_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    data_timestamp=data_timestamp,
                )

            def _fail(message: str, source_chain=None, data_timestamp: str | None = None):
                return fail_with_meta(
                    message,
                    tool_name="trading_data_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    data_timestamp=data_timestamp,
                )

            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'dragon_tiger': '龙虎榜查询（可选 date, stock_code）',
                        'block_trades': '大宗交易查询（可选 date, stock_code）',
                        'institutional_flow': '机构资金流向（可选 date）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['trading_data_manager'])

            elif action == 'dragon_tiger':
                date_raw = kwargs.get('date', datetime.now().strftime('%Y-%m-%d'))
                limit = kwargs.get('limit', 50)
                source_chain = ['trading_data_manager']

                if hasattr(date_raw, 'toordinal'):
                    date_val = date_raw
                else:
                    date_val = datetime.strptime(str(date_raw)[:10], '%Y-%m-%d').date()

                async with db.acquire() as conn:
                    try:
                        rows = await conn.fetch(
                            """SELECT code, trade_date, reason, buy_amount, sell_amount,
                                      (COALESCE(buy_amount,0) - COALESCE(sell_amount,0)) AS net_buy, buyer_type
                               FROM dragon_tiger
                               WHERE trade_date = $1
                               ORDER BY (COALESCE(buy_amount,0) - COALESCE(sell_amount,0)) DESC NULLS LAST
                               LIMIT $2""",
                            date_val, limit
                        )
                        data = [dict(row) for row in rows]
                        if data:
                            source_chain.append('db.dragon_tiger')
                    except Exception:
                        rows = await conn.fetch(
                            """SELECT stock_code AS code, trade_date, reason,
                                      net_amount AS net_buy, NULL AS buy_amount,
                                      NULL AS sell_amount, NULL AS buyer_type
                               FROM dragon_tiger_list
                               WHERE trade_date = $1
                               ORDER BY COALESCE(net_amount, 0) DESC NULLS LAST
                               LIMIT $2""",
                            date_val, limit
                        )
                        data = [dict(row) for row in rows]
                        if data:
                            source_chain.append('db.dragon_tiger_list')

                # 如果当日无数据，尝试获取最近的龙虎榜数据
                if not data:
                    logger.info(f"[TradingData] {date_val} 无龙虎榜数据，查询最近数据")
                    async with db.acquire() as conn:
                        try:
                            rows = await conn.fetch(
                                """SELECT code, trade_date, reason, buy_amount, sell_amount,
                                          (COALESCE(buy_amount,0) - COALESCE(sell_amount,0)) AS net_buy, buyer_type
                                   FROM dragon_tiger
                                   WHERE trade_date <= $1
                                   ORDER BY trade_date DESC, (COALESCE(buy_amount,0) - COALESCE(sell_amount,0)) DESC NULLS LAST
                                   LIMIT $2""",
                                date_val, limit
                            )
                            data = [dict(row) for row in rows]
                            if data:
                                date_val = data[0]['trade_date']
                                source_chain.append('db.dragon_tiger')
                        except Exception:
                            rows = await conn.fetch(
                                """SELECT stock_code AS code, trade_date, reason,
                                          net_amount AS net_buy, NULL AS buy_amount,
                                          NULL AS sell_amount, NULL AS buyer_type
                                   FROM dragon_tiger_list
                                   WHERE trade_date <= $1
                                   ORDER BY trade_date DESC, COALESCE(net_amount, 0) DESC NULLS LAST
                                   LIMIT $2""",
                                date_val, limit
                            )
                            data = [dict(row) for row in rows]
                            if data:
                                date_val = data[0]['trade_date']
                                source_chain.append('db.dragon_tiger_list')

                if data:
                    total_buy = sum(row.get('buy_amount', 0) or 0 for row in data)
                    total_sell = sum(row.get('sell_amount', 0) or 0 for row in data)
                    net_buy = total_buy - total_sell

                    analysis = {
                        'totalBuy': float(total_buy),
                        'totalSell': float(total_sell),
                        'netBuy': float(net_buy),
                        'marketSentiment': 'bullish' if net_buy > 0 else 'bearish',
                        'activeStocks': len(data)
                    }
                else:
                    # DB 全部无数据，Tushare 降级
                    logger.info(f"[TradingData] DB无龙虎榜数据，尝试Tushare")
                    try:
                        from ..fund_flow import get_dragon_tiger
                        date_str = str(date_val).replace('-', '')
                        dt_res = get_dragon_tiger(date=date_str)
                        if dt_res.get('success') and dt_res.get('data'):
                            source_chain.extend(dt_res.get('source_chain') or ['fund_flow.get_dragon_tiger'])
                            dt_data = dt_res['data']
                            if isinstance(dt_data, list):
                                for item in dt_data:
                                    buy_amt = float(item.get('buyAmount', 0) or 0)
                                    sell_amt = float(item.get('sellAmount', 0) or 0)
                                    net_amt = float(item.get('netAmount', 0) or 0) or (buy_amt - sell_amt)
                                    data.append({
                                        'code': item.get('code', ''),
                                        'tradeDate': str(date_val),
                                        'reason': item.get('reason', ''),
                                        'buyAmount': buy_amt,
                                        'sellAmount': sell_amt,
                                        'netBuy': net_amt,
                                        'buyerType': None,
                                    })
                    except Exception as e:
                        logger.warning(f"[TradingData] Tushare龙虎榜也失败: {e}")

                    if data:
                        total_buy = sum(row.get('buyAmount') or row.get('buy_amount') or 0 for row in data)
                        total_sell = sum(row.get('sellAmount') or row.get('sell_amount') or 0 for row in data)
                        net_buy = total_buy - total_sell
                        analysis = {
                            'totalBuy': float(total_buy),
                            'totalSell': float(total_sell),
                            'netBuy': float(net_buy),
                            'marketSentiment': 'bullish' if net_buy > 0 else 'bearish',
                            'activeStocks': len(data)
                        }
                    else:
                        analysis = {
                            'totalBuy': 0,
                            'totalSell': 0,
                            'netBuy': 0,
                            'marketSentiment': 'neutral',
                            'activeStocks': 0
                        }

                return _ok({
                    'date': str(date_val),
                    'data': data,
                    'analysis': analysis,
                    'message': '返回最近龙虎榜数据' if not data or str(date_val) != str(kwargs.get('date', datetime.now().strftime('%Y-%m-%d'))[:10]) else None
                }, source_chain=_dedupe_chain(source_chain), data_timestamp=str(date_val))

            elif action == 'block_trades':
                code = kwargs.get('code')
                days = kwargs.get('days', 5)
                limit = int(kwargs.get('limit', 50) or 50)
                date_raw = kwargs.get('date')
                source_chain = ['trading_data_manager']

                trades = []
                if code:
                    async with db.acquire() as conn:
                        rows = await conn.fetch(
                            """SELECT * FROM block_trades
                               WHERE code = $1 AND trade_date >= date('now', '-' || $2 || ' days')
                               ORDER BY trade_date DESC, trade_amount DESC""",
                            code, int(days)
                        )
                        trades = [dict(row) for row in rows]
                        if trades:
                            source_chain.append('db.block_trades')

                if trades:
                    total_amount = sum(t.get('trade_amount', 0) for t in trades)
                    avg_price = sum(t.get('trade_price', 0) for t in trades) / len(trades)

                    klines = await db.get_klines(code, limit=1)
                    current_price = klines[0]['close'] if klines else 0

                    premium = (avg_price - current_price) / current_price if current_price > 0 else 0

                    analysis = {
                        'totalTrades': len(trades),
                        'totalAmount': float(total_amount),
                        'avgPrice': float(avg_price),
                        'currentPrice': float(current_price),
                        'premium': f"{premium*100:.2f}%",
                        'signal': 'positive' if premium > 0 else ('negative' if premium < -0.05 else 'neutral')
                    }
                else:
                    analysis = {
                        'totalTrades': 0,
                        'totalAmount': 0,
                        'signal': 'no_data'
                    }
                    try:
                        from ..fund_flow import get_block_trades as _get_block_trades

                        tool_res = _get_block_trades(
                            date=str(date_raw or ""),
                            stock_code=str(code or ""),
                            limit=limit,
                        )
                        if tool_res.get('success'):
                            tool_trades = tool_res.get('data') or []
                            if tool_trades:
                                trades = tool_trades
                                source_chain.extend(tool_res.get('source_chain') or ['fund_flow.get_block_trades'])
                                total_amount = sum(float(t.get('amount') or t.get('trade_amount') or 0) for t in trades)
                                price_values = [
                                    float(t.get('price') or t.get('trade_price') or 0)
                                    for t in trades
                                    if (t.get('price') or t.get('trade_price')) is not None
                                ]
                                avg_price = sum(price_values) / len(price_values) if price_values else 0.0
                                analysis = {
                                    'totalTrades': len(trades),
                                    'totalAmount': float(total_amount),
                                    'avgPrice': float(avg_price),
                                    'signal': 'neutral' if total_amount > 0 else 'no_data',
                                }
                                return _ok({
                                    'code': code,
                                    'days': days,
                                    'trades': trades[:limit],
                                    'analysis': analysis,
                                    'data_quality': tool_res.get('data_quality'),
                                    'source_chain': tool_res.get('source_chain'),
                                    'fallback_reason': tool_res.get('fallback_reason'),
                                    'degraded': bool(tool_res.get('degraded')),
                                }, source_chain=_dedupe_chain(source_chain), data_timestamp=str(date_raw or datetime.now().date()))
                    except Exception as e:
                        logger.warning(f"[TradingData] get_block_trades fallback 失败: {e}")

                return _ok({
                    'code': code,
                    'days': days,
                    'trades': trades,
                    'analysis': analysis
                }, source_chain=_dedupe_chain(source_chain), data_timestamp=str(date_raw or datetime.now().date()))

            elif action == 'institutional_flow':
                code = kwargs.get('code')
                period = kwargs.get('period', 20)
                source_chain = ['trading_data_manager']

                if not code:
                    return _fail('需要提供股票代码', source_chain=source_chain)

                async with db.acquire() as conn:
                    try:
                        rows = await conn.fetch(
                            """SELECT code, trade_date, reason, buy_amount, sell_amount,
                                      (COALESCE(buy_amount,0) - COALESCE(sell_amount,0)) AS net_buy, buyer_type
                               FROM dragon_tiger
                               WHERE code = $1
                               AND trade_date >= date('now', '-' || $2 || ' days')
                               AND (buyer_type = 'institution' OR buyer_type IS NULL)
                               ORDER BY trade_date DESC""",
                            code, period
                        )
                        source_chain.append('db.dragon_tiger')
                    except Exception:
                        rows = await conn.fetch(
                            """SELECT stock_code AS code, trade_date, reason,
                                      NULL AS buy_amount, NULL AS sell_amount,
                                      net_amount AS net_buy, NULL AS buyer_type
                               FROM dragon_tiger_list
                               WHERE stock_code = $1
                               AND trade_date >= date('now', '-' || $2 || ' days')
                               ORDER BY trade_date DESC""",
                            code, period
                        )
                        source_chain.append('db.dragon_tiger_list')
                    institutional_trades = [dict(row) for row in rows]

                if institutional_trades:
                    total_buy = sum(t.get('buy_amount', 0) for t in institutional_trades)
                    total_sell = sum(t.get('sell_amount', 0) for t in institutional_trades)
                    net_flow = total_buy - total_sell

                    flow_analysis = {
                        'totalBuy': float(total_buy),
                        'totalSell': float(total_sell),
                        'netFlow': float(net_flow),
                        'flowDirection': 'inflow' if net_flow > 0 else 'outflow',
                        'strength': 'strong' if abs(net_flow) > total_buy * 0.3 else 'weak',
                        'tradeCount': len(institutional_trades)
                    }
                else:
                    flow_analysis = {
                        'totalBuy': 0,
                        'totalSell': 0,
                        'netFlow': 0,
                        'flowDirection': 'neutral',
                        'strength': 'none',
                        'tradeCount': 0
                    }

                return _ok({
                    'code': code,
                    'period': period,
                    'institutionalFlow': flow_analysis,
                    'trades': institutional_trades[:10]
                }, source_chain=_dedupe_chain(source_chain))

            else:
                return _fail(
                    f'Unknown action: {action}. Supported: help, dragon_tiger, block_trades, institutional_flow',
                    source_chain=['trading_data_manager'],
                )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name='trading_data_manager',
                action=action,
                started_at=start_time,
                source_chain=['trading_data_manager'],
            )
