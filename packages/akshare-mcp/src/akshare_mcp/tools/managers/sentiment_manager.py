"""情绪分析管理器（增强版）"""

from typing import Any
import time

from ...utils import normalize_code
from ...storage import get_db
from ...data_source import data_source
from ..manager_protocol import (
    normalize_manager_payload,
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    ok_with_meta,
)
import logging

logger = logging.getLogger(__name__)

_SECTOR_ALIAS_HINTS = {
    "白酒": ["酿酒", "酒"],
    "酿酒": ["白酒", "酒"],
    "鐧介厭": ["閰块厭", "閰"],
    "閰块厭": ["鐧介厭", "閰"],
    "券商": ["证券", "金融"],
    "银行": ["金融"],
    "保险": ["金融"],
}

def _normalize_sector_key(text: str) -> str:
    value = str(text or "").strip().lower()
    for token in ("行业", "概念", "板块", "ⅰ", "ⅱ", "ⅲ", "ⅳ", "i", "ii", "iii", "iv"):
        value = value.replace(token, "")
    return value.strip()


def _sector_match_score(query: str, candidate: str) -> int:
    q = _normalize_sector_key(query)
    c = _normalize_sector_key(candidate)
    if not q or not c:
        return 0
    if q == c:
        return 100
    if q in c or c in q:
        return 80

    alias_terms = [q]
    for key, values in _SECTOR_ALIAS_HINTS.items():
        if key in q:
            alias_terms.extend(values)
    for term in alias_terms:
        term = _normalize_sector_key(term)
        if term and term in c:
            return 60
    return 0


def _sector_alias_terms(sector: str) -> list[str]:
    base = _normalize_sector_key(sector)
    terms = [base] if base else []
    for key, values in _SECTOR_ALIAS_HINTS.items():
        normalized_key = _normalize_sector_key(key)
        if normalized_key and (normalized_key in base or base in normalized_key):
            terms.extend(_normalize_sector_key(item) for item in values)
    deduped: list[str] = []
    for term in terms:
        if term and term not in deduped:
            deduped.append(term)
    return deduped


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


def register_sentiment_manager(mcp):
    """注册情绪分析管理器工具"""
    
    @mcp.tool()
    async def sentiment_manager(action: str, params: dict | None = None, kwargs: Any = None):
        """情绪分析管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/market_sentiment/stock_sentiment/sector_sentiment
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - market_sentiment: 无需额外参数（返回市场整体情绪指标）
                - stock_sentiment: code(str, 股票代码)
                - sector_sentiment: sector(str, 板块名称, optional)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            sentiment_manager(action="help", kwargs="{}")
            # 市场整体情绪
            sentiment_manager(action="market_sentiment", kwargs="{}")
            # 个股情绪分析
            sentiment_manager(action="stock_sentiment", kwargs='{"code":"600519"}')
            # 板块情绪分析
            sentiment_manager(action="sector_sentiment", kwargs='{"sector":"白酒"}')
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs)
            kwargs = normalize_manager_kwargs(
                kwargs,
                field_aliases={
                    "sector": ("Sector", "industry", "industry_name"),
                },
            )
            _, kwargs = normalize_manager_code(None, kwargs)

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="sentiment_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="sentiment_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'market_sentiment': '市场整体情绪分析',
                        'stock_sentiment': '个股情绪分析（需要 code）',
                        'sector_sentiment': '板块情绪分析（需要 sector）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['sentiment_manager'])
            
            elif action == 'market_sentiment':
                # 计算市场整体情绪
                # 数据源优先级: DB → Tushare → AkShare
                min_market_sample = 100
                up_count = 0
                down_count = 0
                total_count = 0
                data_source_info = '数据库'
                source_chain = ['sentiment_manager']
                try:
                    async with db.acquire() as conn:
                        # 取每只股票最新一条行情
                        up_count = await conn.fetchval(
                            """
                            WITH latest AS (
                                SELECT code, change_pct
                                FROM (
                                    SELECT code, change_pct,
                                           ROW_NUMBER() OVER (PARTITION BY code ORDER BY time DESC) AS rn
                                    FROM stock_quotes
                                ) ranked
                                WHERE rn = 1
                            )
                            SELECT COUNT(*) FROM latest WHERE change_pct > 0
                            """
                        ) or 0
                        down_count = await conn.fetchval(
                            """
                            WITH latest AS (
                                SELECT code, change_pct
                                FROM (
                                    SELECT code, change_pct,
                                           ROW_NUMBER() OVER (PARTITION BY code ORDER BY time DESC) AS rn
                                    FROM stock_quotes
                                ) ranked
                                WHERE rn = 1
                            )
                            SELECT COUNT(*) FROM latest WHERE change_pct < 0
                            """
                        ) or 0
                        total_count = int(up_count) + int(down_count)
                        source_chain.append('db.stock_quotes')
                except Exception as e:
                    logger.warning(f"[Sentiment] 数据库读取失败，切换数据源降级: {e}")
                    data_source_info = f'数据库不可用: {e}'
                
                # 方案2: 如果数据库样本过小，从数据源获取（优先级: Tushare → AkShare批量 → 逐只K线）
                if total_count < min_market_sample:
                    logger.info("[Sentiment] 数据库样本不足(%s)，从数据源获取市场情绪", total_count)
                    
                    # 优先尝试 Tushare daily 获取全市场涨跌
                    try:
                        ts_pro = data_source.get_tushare_pro()
                        if ts_pro:
                            import datetime as _dt_sent
                            base = _dt_sent.datetime.now()
                            for days_back in range(7):
                                check_date = (base - _dt_sent.timedelta(days=days_back)).strftime('%Y%m%d')
                                try:
                                    df = ts_pro.daily(trade_date=check_date, fields='ts_code,pct_chg')
                                    if df is not None and not df.empty and len(df) > 100:
                                        up_count = int((df['pct_chg'] > 0).sum())
                                        down_count = int((df['pct_chg'] < 0).sum())
                                        total_count = up_count + down_count
                                        data_source_info = f'Tushare daily ({check_date})'
                                        source_chain.append('tushare.daily')
                                        logger.info(f"[Sentiment] Tushare: 涨{up_count} 跌{down_count}")
                                        break
                                except Exception:
                                    continue
                    except Exception as e:
                        logger.warning(f"[Sentiment] Tushare批量行情失败: {e}")
                    
                    # 降级: AkShare 批量行情
                    if total_count < min_market_sample:
                        try:
                            import akshare as ak
                            df = ak.stock_zh_a_spot_em()
                            if df is not None and not df.empty:
                                chg_col = '涨跌幅'
                                if chg_col in df.columns:
                                    up_count = int(len(df[df[chg_col] > 0]))
                                    down_count = int(len(df[df[chg_col] < 0]))
                                    total_count = up_count + down_count
                                    data_source_info = 'AkShare实时行情'
                                    source_chain.append('akshare.stock_zh_a_spot_em')
                                    logger.info(f"[Sentiment] AkShare批量行情: 涨{up_count} 跌{down_count}")
                        except Exception as e:
                            logger.warning(f"[Sentiment] AkShare批量行情失败: {e}")
                
                # 方案3: AkShare批量也失败，逐只K线兜底
                if total_count == 0:
                    sample_stocks = ['600519', '000001', '600036', '601318', '000858', 
                                   '601166', '600887', '601288', '600030', '601398']
                    
                    up_count = 0
                    down_count = 0
                    
                    for code in sample_stocks:
                        try:
                            quote_data = data_source.get_kline(code, 'daily', 1)
                            if quote_data and len(quote_data) > 0:
                                latest = quote_data[-1]
                                change_pct = latest.get('change_pct', 0)
                                if change_pct > 0:
                                    up_count += 1
                                elif change_pct < 0:
                                    down_count += 1
                        except Exception as e:
                            logger.warning(f"[Sentiment] 获取 {code} 行情失败: {e}")
                            continue
                    
                    total_count = up_count + down_count
                    data_source_info = '实时数据源(样本)'
                    source_chain.append('data_source.get_kline')
                
                # 计算情绪指标
                if total_count > 0:
                    up_ratio = up_count / total_count
                    sentiment_score = up_ratio * 100
                    
                    if sentiment_score >= 70:
                        sentiment = 'bullish'
                        description = '市场情绪乐观'
                    elif sentiment_score >= 55:
                        sentiment = 'slightly_bullish'
                        description = '市场情绪偏乐观'
                    elif sentiment_score >= 45:
                        sentiment = 'neutral'
                        description = '市场情绪中性'
                    elif sentiment_score >= 30:
                        sentiment = 'slightly_bearish'
                        description = '市场情绪偏悲观'
                    else:
                        sentiment = 'bearish'
                        description = '市场情绪悲观'
                else:
                    # 方案3: 完全无数据时，返回中性
                    sentiment_score = 50
                    sentiment = 'neutral'
                    description = '数据不足，返回中性情绪'
                    data_source_info = '默认值'
                    up_count = 0
                    down_count = 0
                    up_ratio = 0.5
                
                return _ok({
                    'sentiment': sentiment,
                    'score': float(sentiment_score),
                    'description': description,
                    'indicators': {
                        'up_count': up_count,
                        'down_count': down_count,
                        'up_ratio': f"{up_ratio*100:.2f}%" if total_count > 0 else "50.00%",
                        'sample_size': total_count,
                        'data_source': data_source_info
                    },
                    # P2-4.5.7 fix: 样本数<50 时显式标 warning(诊断报告 §4.5.7)
                    # 历史问题:数据库样本极少时,sentiment_score 仍按 0~100 输出,AI 不知是统计偏差
                    'warnings': (
                        [{
                            'code': 'low_sample_size',
                            'message': f'sample_size={total_count} 低于稳健阈值 50,情绪打分置信度低,建议结合北向资金/恐贪指数综合判断',
                            'severity': 'warning' if total_count >= 10 else 'critical',
                        }] if total_count < 50 else []
                    ),
                    'reliable': total_count >= 50,
                    'sample_threshold': 50,
                }, source_chain=_dedupe_chain(source_chain))
            
            elif action == 'stock_sentiment':
                code = kwargs.get('code')
                if not code:
                    return _fail('需要提供股票代码', source_chain=['sentiment_manager'])
                
                code = normalize_code(code)
                source_chain = ['sentiment_manager']
                
                # 获取K线数据分析情绪
                try:
                    klines = await db.get_klines(code, limit=20)
                    if klines:
                        source_chain.append('db.get_klines')
                except Exception as e:
                    logger.warning(f"[Sentiment] DB获取 {code} K线失败，使用数据源降级: {e}")
                    klines = []
                if not klines:
                    klines = data_source.get_kline(code, 'daily', 20)
                    if klines:
                        source_chain.append('data_source.get_kline')
                
                if not klines or len(klines) < 5:
                    return _ok({
                        'code': code,
                        'sentiment': 'neutral',
                        'score': 50,
                        'description': '数据不足'
                    }, source_chain=_dedupe_chain(source_chain))
                
                # 计算涨跌天数
                up_days = sum(1 for k in klines if k.get('close', 0) > k.get('open', 0))
                down_days = len(klines) - up_days
                
                # 计算成交量变化
                volumes = [k.get('volume', 0) for k in klines]
                avg_volume = sum(volumes) / len(volumes) if volumes else 0
                recent_volume = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
                volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1
                
                # 情绪评分
                sentiment_score = (up_days / len(klines)) * 60 + (min(volume_ratio, 2) / 2) * 40
                
                if sentiment_score >= 70:
                    sentiment = 'bullish'
                    description = '个股情绪乐观'
                elif sentiment_score >= 55:
                    sentiment = 'slightly_bullish'
                    description = '个股情绪偏乐观'
                elif sentiment_score >= 45:
                    sentiment = 'neutral'
                    description = '个股情绪中性'
                elif sentiment_score >= 30:
                    sentiment = 'slightly_bearish'
                    description = '个股情绪偏悲观'
                else:
                    sentiment = 'bearish'
                    description = '个股情绪悲观'
                
                return _ok({
                    'code': code,
                    'sentiment': sentiment,
                    'score': float(sentiment_score),
                    'description': description,
                    'indicators': {
                        'up_days': up_days,
                        'down_days': down_days,
                        'volume_ratio': float(volume_ratio),
                        'volume_status': 'active' if volume_ratio > 1.2 else ('weak' if volume_ratio < 0.8 else 'normal')
                    }
                }, source_chain=_dedupe_chain(source_chain))
            
            elif action == 'sector_sentiment':
                sector = kwargs.get('sector')
                if not sector:
                    return _fail('需要提供板块名称', source_chain=['sentiment_manager'])
                source_chain = ['sentiment_manager']
                
                # 获取板块内股票的情绪
                try:
                    async with db.acquire() as conn:
                        stock_cols = await conn.fetch("SELECT name AS column_name FROM pragma_table_info('stocks')")
                        columns = {str(row["column_name"]) for row in stock_cols or []}
                        code_col = "stock_code" if "stock_code" in columns else ("code" if "code" in columns else "")
                        industry_col = "industry" if "industry" in columns else ("sector" if "sector" in columns else "")
                        stocks = []
                        if code_col and industry_col:
                            alias_terms = _sector_alias_terms(sector)
                            clauses = []
                            params = []
                            for idx, term in enumerate(alias_terms[:5], start=1):
                                clauses.append(f"{industry_col} LIKE ${idx}")
                                params.append(f"%{term}%")
                            where = " OR ".join(clauses) or "1 = 0"
                            stocks = await conn.fetch(
                                f"SELECT {code_col} AS code FROM stocks WHERE {where} LIMIT 20",
                                *params,
                            )
                        else:
                            stocks = []
                        if stocks:
                            source_chain.append('db.stocks')
                except Exception as e:
                    logger.warning(f"[Sentiment] DB获取板块 {sector} 股票失败，尝试板块接口降级: {e}")
                    stocks = []

                if not stocks:
                    try:
                        from ..market_blocks import get_market_blocks, get_block_stocks

                        matched_block_code = None
                        matched_score = 0
                        for block_type in ('industry', 'concept'):
                            blocks_res = await get_market_blocks(block_type=block_type, limit=200)
                            if not blocks_res.get('success'):
                                continue
                            source_chain.append('market_blocks.get_market_blocks')
                            for block in blocks_res.get('data', {}).get('blocks', []):
                                block_name = block.get('name') or block.get('blockName') or block.get('block_name')
                                score = _sector_match_score(sector, str(block_name or ''))
                                if score > matched_score:
                                    matched_score = score
                                    matched_block_code = block.get('code') or block.get('blockCode') or block.get('block_code')
                            if matched_block_code:
                                stocks_res = await get_block_stocks(matched_block_code)
                                if stocks_res.get('success'):
                                    source_chain.append('market_blocks.get_block_stocks')
                                    stocks = [
                                        {'code': item.get('code') or item.get('stock_code')}
                                        for item in stocks_res.get('data', {}).get('stocks', [])[:20]
                                        if item.get('code') or item.get('stock_code')
                                    ]
                                    break
                    except Exception as e:
                        logger.warning(f"[Sentiment] 板块接口降级失败: {e}")
                
                if not stocks:
                    return _ok(
                        {
                            'sector': sector,
                            'sentiment': 'neutral',
                            'score': 50.0,
                            'stock_count': 0,
                            'degraded': True,
                            'fallback_reason': 'sector constituents not found',
                        },
                        source_chain=_dedupe_chain(source_chain),
                    )
                
                sentiment_scores = []
                for stock in stocks:
                    result = await sentiment_manager('stock_sentiment', params={'code': stock['code']})
                    if result.get('success'):
                        sentiment_scores.append(result['data']['score'])
                        source_chain.extend(result.get('meta', {}).get('source_chain') or [])
                
                if sentiment_scores:
                    avg_score = sum(sentiment_scores) / len(sentiment_scores)
                    
                    if avg_score >= 70:
                        sentiment = 'bullish'
                    elif avg_score >= 55:
                        sentiment = 'slightly_bullish'
                    elif avg_score >= 45:
                        sentiment = 'neutral'
                    elif avg_score >= 30:
                        sentiment = 'slightly_bearish'
                    else:
                        sentiment = 'bearish'
                else:
                    avg_score = 50
                    sentiment = 'neutral'
                
                return _ok({
                    'sector': sector,
                    'sentiment': sentiment,
                    'score': float(avg_score),
                    'stock_count': len(stocks)
                }, source_chain=_dedupe_chain(source_chain))
            
            else:
                return _fail(
                    f'Unknown action: {action}. Supported: help, market_sentiment, stock_sentiment, sector_sentiment',
                    source_chain=['sentiment_manager'],
                )
        except Exception as e:
            logger.error(f"[SentimentManager] Error: {e}")
            return fail_with_meta(
                str(e),
                tool_name='sentiment_manager',
                action=action,
                started_at=start_time,
                source_chain=['sentiment_manager'],
            )
