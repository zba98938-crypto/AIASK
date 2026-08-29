"""决策管理器 - AI辅助决策（增强版）"""

from typing import Any
import json
import logging
import time
from datetime import datetime

import numpy as np

from ...data_source import data_source
from ...storage import get_db
from ...tools.market.helpers import get_stock_list_cached
from ...utils import normalize_code
from ..manager_protocol import (
    normalize_manager_payload,
    extract_common_meta,
    fail_with_meta,
    normalize_manager_kwargs,
    ok_with_meta,
)

logger = logging.getLogger(__name__)


def register_decision_manager(mcp):
    """注册决策管理器工具"""
    
    @mcp.tool()
    async def decision_manager(action: str, params: dict | None = None, kwargs: Any = None, code: str | None = None, codes: list[str] | None = None, weights: list[float] | None = None, investment_style: str | None = None, criteria: dict | None = None, limit: int | None = None, portfolio_id: str | int | None = None):
        """决策管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/analyze/recommend/portfolio_advice
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - analyze: code(str, 股票代码)
                - recommend: investment_style(str, optional, "aggressive"/"balanced"/"conservative")
                - portfolio_advice: codes(list[str]), weights(list[float], optional)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            decision_manager(action="help", kwargs="{}")
            # 综合分析
            decision_manager(action="analyze", kwargs='{"code":"600519"}')
            # 推荐股票
            decision_manager(action="recommend", kwargs='{"investment_style":"balanced"}')
            # 组合建议
            decision_manager(action="portfolio_advice", kwargs='{"codes":["600519","000858","002304"],"weights":[0.4,0.3,0.3]}')
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "code": code,
                    "codes": codes,
                    "weights": weights,
                    "investment_style": investment_style,
                    "criteria": criteria,
                    "limit": limit,
                    "portfolio_id": portfolio_id,
                },
            )
            kwargs = normalize_manager_kwargs(kwargs)

            # 统一可选参数（向后兼容）
            meta_defaults = {
                'as_of': '',
                'adjust': '',
                'price_source_policy': 'auto',
                'explain': True,
                'strict_mode': False,
            }
            common_meta = extract_common_meta(kwargs, defaults=meta_defaults)
            explain = common_meta['explain']

            def _ok(data: dict, source_chain=None, data_timestamp: str | None = None):
                return ok_with_meta(
                    data,
                    tool_name='decision_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    data_timestamp=data_timestamp,
                    extra_meta=common_meta,
                )

            def _fail(message: str, source_chain=None, data_timestamp: str | None = None):
                return fail_with_meta(
                    message,
                    tool_name='decision_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    data_timestamp=data_timestamp,
                    extra_meta=common_meta,
                )

            # action 可能也在 kwargs 里（部分 MCP 客户端把所有参数放进 kwargs）
            if not action and kwargs.get("action"):
                action = kwargs.get("action")
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'analyze': '综合分析（需要 code）',
                        'recommend': '推荐股票（可选 criteria, limit）',
                        'portfolio_advice': '组合建议（支持 portfolio_id 或 codes + weights）',
                        'help': '显示帮助信息',
                    }
                })
            
            elif action == 'analyze':
                code = kwargs.get('code')
                if not code:
                    return _fail('需要提供股票代码（可传 code / stock_code / symbol，或放在 kwargs 的 JSON 中）')
                
                code = normalize_code(code)
                
                # 自动获取K线数据
                klines = await db.get_klines(code, limit=100)
                if not klines:
                    logger.info(f"[DecisionManager] Fetching klines for {code}")
                    klines = data_source.get_kline(code, 'daily', 100)
                    if klines:
                        try:
                            await db.save_klines(code, klines)
                        except Exception:
                            pass
                
                if not klines or len(klines) < 20:
                    return _fail(f'K线数据不足，无法分析（需要至少20天数据，当前{len(klines) if klines else 0}天）')
                
                prices = np.array([k['close'] for k in klines])
                volumes = np.array([k['volume'] for k in klines])
                
                ma5 = np.mean(prices[-5:])
                ma20 = np.mean(prices[-20:])
                ma60 = np.mean(prices[-60:]) if len(prices) >= 60 else ma20
                
                current_price = prices[-1]
                
                trend_score = 0
                if current_price > ma5 > ma20 > ma60:
                    trend = 'strong_uptrend'
                    trend_score = 80
                elif current_price > ma5 > ma20:
                    trend = 'uptrend'
                    trend_score = 60
                elif current_price < ma5 < ma20 < ma60:
                    trend = 'strong_downtrend'
                    trend_score = 20
                elif current_price < ma5 < ma20:
                    trend = 'downtrend'
                    trend_score = 40
                else:
                    trend = 'sideways'
                    trend_score = 50
                
                financials = await db.get_financials(code, limit=1)

                # P2-2: 区分“缺失值(None)”与“真实数值0”，避免使用 or 0 掩盖数据质量问题
                latest_financial = {}
                if isinstance(financials, list) and financials:
                    latest_financial = financials[0] if isinstance(financials[0], dict) else {}
                elif isinstance(financials, dict):
                    latest_financial = financials

                def _to_float_or_none(v):
                    if v is None:
                        return None
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None

                roe = _to_float_or_none(latest_financial.get('roe')) if latest_financial else None
                pe_ratio = _to_float_or_none(latest_financial.get('pe_ratio')) if latest_financial else None
                debt_ratio = _to_float_or_none(latest_financial.get('debt_ratio')) if latest_financial else None
                pb_ratio = _to_float_or_none(latest_financial.get('pb_ratio')) if latest_financial else None

                required_fin_fields = ['roe', 'pe_ratio', 'debt_ratio', 'pb_ratio']
                metric_map = {
                    'roe': roe,
                    'pe_ratio': pe_ratio,
                    'debt_ratio': debt_ratio,
                    'pb_ratio': pb_ratio,
                }
                missing_fields = [k for k in required_fin_fields if metric_map.get(k) is None]
                completeness = (len(required_fin_fields) - len(missing_fields)) / len(required_fin_fields)

                # 缺失字段采用“降权”而非“硬惩罚”，最多扣 15 分
                score_penalty = min(15.0, float(len(missing_fields)) * 4.0)

                fundamental_score = 50
                if latest_financial:
                    if roe is not None and pe_ratio is not None and debt_ratio is not None and roe > 15 and pe_ratio < 25 and debt_ratio < 0.5:
                        fundamental_score = 80
                    elif roe is not None and pe_ratio is not None and roe > 10 and pe_ratio < 35:
                        fundamental_score = 65
                    elif (roe is not None and roe < 5) or (pe_ratio is not None and pe_ratio > 50):
                        fundamental_score = 30

                avg_volume = np.mean(volumes[-20:])
                recent_volume = np.mean(volumes[-5:])
                volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1

                sentiment_score = 50
                if volume_ratio > 1.5:
                    sentiment_score = 70
                elif volume_ratio < 0.7:
                    sentiment_score = 40

                # 估值评分（与 smart_stock_diagnosis 口径对齐）
                valuation_score = 50
                pe = pe_ratio
                pb = pb_ratio

                if pe is not None and pe > 0:
                    if pe < 15:
                        valuation_score += 20
                    elif pe > 50:
                        valuation_score -= 20
                    else:
                        valuation_score += 10

                if pb is not None and pb > 0:
                    if pb < 2:
                        valuation_score += 15
                    elif pb > 5:
                        valuation_score -= 15

                valuation_score = max(0, min(100, valuation_score))

                # 综合评分：与 smart_stock_diagnosis 对齐（0.3/0.3/0.25/0.15）
                raw_total_score = (
                    trend_score * 0.3 +
                    fundamental_score * 0.3 +
                    valuation_score * 0.25 +
                    sentiment_score * 0.15
                )
                total_score = max(0.0, raw_total_score - score_penalty)
                
                # 新口径 recommendation（buy/hold/wait/sell）
                if total_score >= 75:
                    recommendation = 'buy'
                    recommendation_text = '强烈推荐买入'
                elif total_score >= 60:
                    recommendation = 'hold'
                    recommendation_text = '可以持有或适量买入'
                elif total_score >= 45:
                    recommendation = 'wait'
                    recommendation_text = '观望为主'
                else:
                    recommendation = 'sell'
                    recommendation_text = '建议卖出或回避'

                # 旧口径 decision（向后兼容）
                if total_score >= 75:
                    decision = 'strong_buy'
                    confidence = 'high'
                    reason = '技术面、基本面、估值、情绪面均表现良好'
                elif total_score >= 60:
                    decision = 'buy'
                    confidence = 'medium'
                    reason = '整体表现较好，建议持有或逢低布局'
                elif total_score >= 30:
                    decision = 'sell'
                    confidence = 'medium'
                    reason = '表现偏弱，建议减仓或观望'
                else:
                    decision = 'strong_sell'
                    confidence = 'high'
                    reason = '多方面表现不佳，建议清仓'
                
                analysis_date = datetime.now().strftime('%Y-%m-%d')
                payload = {
                    'code': code,
                    'recommendation': recommendation,
                    'recommendation_text': recommendation_text,
                    'decision': decision,
                    'confidence': confidence,
                    'overall_score': float(total_score),
                    'total_score': float(total_score),
                    'raw_total_score': float(raw_total_score),
                    'reason': reason,
                    'data_quality': {
                        'missing_fields': missing_fields,
                        'financial_data_completeness': float(round(completeness, 4)),
                        'score_penalty': float(score_penalty),
                    },
                    'analysis': {
                        'technical': {
                            'score': float(trend_score),
                            'trend': trend,
                            'current_price': float(current_price),
                            'ma5': float(ma5),
                            'ma20': float(ma20),
                            'ma60': float(ma60),
                        },
                        'fundamental': {
                            'score': float(fundamental_score),
                            'roe': roe,
                            'pe_ratio': pe_ratio,
                            'debt_ratio': debt_ratio,
                            'data_quality': {
                                'missing_fields': missing_fields,
                                'completeness': float(round(completeness, 4)),
                            },
                        },
                        'valuation': {
                            'score': float(valuation_score),
                            'pe_ratio': pe,
                            'pb_ratio': pb,
                            'data_quality': {
                                'missing_fields': [f for f in ['pe_ratio', 'pb_ratio'] if metric_map.get(f) is None],
                            },
                        },
                        'sentiment': {
                            'score': float(sentiment_score),
                            'volume_ratio': float(volume_ratio),
                            'status': 'active' if volume_ratio > 1.2 else ('weak' if volume_ratio < 0.8 else 'normal')
                        }
                    },
                    'scores': {
                        'technical': float(trend_score),
                        'fundamental': float(fundamental_score),
                        'valuation': float(valuation_score),
                        'sentiment': float(sentiment_score),
                    },
                    'analysis_date': analysis_date,
                    'risk_warning': '投资有风险，决策仅供参考' if total_score < 60 else None
                }

                if explain:
                    payload['diagnostic'] = {
                        'trace': [
                            f'trend_score={trend_score}',
                            f'fundamental_score={fundamental_score}',
                            f'valuation_score={valuation_score}',
                            f'sentiment_score={sentiment_score}',
                            f'raw_total_score={round(raw_total_score, 2)}',
                            f'score_penalty={score_penalty}',
                            f'missing_fields={missing_fields}',
                            f'total_score={round(total_score, 2)}',
                            f'recommendation={recommendation}',
                        ]
                    }

                return _ok(payload, data_timestamp=analysis_date)
            
            elif action == 'recommend':
                criteria = kwargs.get('criteria', {})
                if isinstance(criteria, str):
                    try:
                        criteria = json.loads(criteria or "{}")
                    except Exception:
                        criteria = {}
                if not isinstance(criteria, dict):
                    criteria = {}

                def _to_int(v, default):
                    try:
                        return int(v)
                    except Exception:
                        return int(default)

                def _to_float(v, default=0.0):
                    try:
                        return float(v)
                    except Exception:
                        return float(default)

                limit = max(1, _to_int(kwargs.get('limit', criteria.get('limit', 10)), 10))
                min_score = _to_float(criteria.get('min_score', kwargs.get('min_score', 60)), 60.0)
                sectors = criteria.get('sectors', kwargs.get('sector_filter', [])) or []
                universe_limit = max(limit, _to_int(kwargs.get('universe_limit', criteria.get('universe_limit', limit * 20)), limit * 20))
                universe_limit = min(universe_limit, 3000)

                if isinstance(sectors, str):
                    sectors = [s.strip() for s in sectors.split(',') if s.strip()]
                sector_set = {str(s).strip() for s in sectors if str(s).strip()}

                codes = kwargs.get('codes', criteria.get('codes', [])) or []
                if isinstance(codes, str):
                    codes = [c.strip() for c in codes.split(',') if c.strip()]

                keyword = str(kwargs.get('keyword', criteria.get('keyword', '')) or '').strip()
                liquidity_filter = criteria.get('liquidity_filter', kwargs.get('liquidity_filter', {})) or {}
                if isinstance(liquidity_filter, str):
                    try:
                        liquidity_filter = json.loads(liquidity_filter or "{}")
                    except Exception:
                        liquidity_filter = {}
                if not isinstance(liquidity_filter, dict):
                    liquidity_filter = {}

                min_market_cap = _to_float(
                    liquidity_filter.get('min_market_cap', liquidity_filter.get('market_cap_min', 0.0)),
                    0.0,
                )

                candidate_codes = []
                candidate_rows = []
                filter_chain = []
                source_method = 'user_codes'

                if isinstance(codes, list) and codes:
                    candidate_codes = [normalize_code(c) for c in codes if isinstance(c, str) and c.strip()]
                    filter_chain.append({'step': 'source', 'method': 'user_codes', 'count': len(candidate_codes)})
                else:
                    # 1) 优先: db.search_stocks（动态候选池，便于按条件检索与测试桩控制）
                    if hasattr(db, 'search_stocks'):
                        try:
                            candidate_rows = await db.search_stocks(keyword=keyword, limit=universe_limit)
                            if candidate_rows:
                                source_method = 'db.search_stocks'
                        except Exception:
                            candidate_rows = []

                    # 2) 降级: get_stock_list_cached（全A股列表）
                    if not candidate_rows:
                        try:
                            stock_list, _cached = get_stock_list_cached()
                            if stock_list:
                                candidate_rows = stock_list[:universe_limit]
                                source_method = 'get_stock_list'
                        except Exception:
                            candidate_rows = []

                    # 3) 降级: SQL 查询
                    if not candidate_rows and hasattr(db, 'acquire'):
                        try:
                            async with db.acquire() as conn:
                                rows = await conn.fetch(
                                    """
                                    SELECT code, stock_name, industry, market_cap
                                    FROM stocks
                                    ORDER BY market_cap DESC NULLS LAST
                                    LIMIT $1
                                    """,
                                    universe_limit,
                                )
                            candidate_rows = []
                            for r in rows:
                                row_dict = dict(r)
                                mc = row_dict.get('market_cap')
                                candidate_rows.append({
                                    'code': row_dict.get('code'),
                                    'name': row_dict.get('stock_name'),
                                    'industry': row_dict.get('industry'),
                                    'market_cap': float(mc) if mc else None,
                                })
                            if candidate_rows:
                                source_method = 'sql_stocks_table'
                        except Exception:
                            candidate_rows = []

                    filter_chain.append({'step': 'source', 'method': source_method, 'count': len(candidate_rows)})

                    if sector_set:
                        before = len(candidate_rows)
                        candidate_rows = [
                            r for r in candidate_rows
                            if str((r or {}).get('industry', '')).strip() in sector_set
                        ]
                        filter_chain.append({'step': 'sector_filter', 'before': before, 'after': len(candidate_rows)})

                    if min_market_cap > 0:
                        before = len(candidate_rows)
                        candidate_rows = [
                            r for r in candidate_rows
                            if _to_float((r or {}).get('market_cap', 0.0), 0.0) >= min_market_cap
                        ]
                        filter_chain.append({'step': 'market_cap_filter', 'before': before, 'after': len(candidate_rows)})

                    seen_codes = set()
                    for row in candidate_rows:
                        code = normalize_code((row or {}).get('code') or (row or {}).get('stock_code') or '')
                        if code and code not in seen_codes:
                            seen_codes.add(code)
                            candidate_codes.append(code)
                    filter_chain.append({'step': 'dedup', 'count': len(candidate_codes)})

                fallback_used = source_method in ('sql_stocks_table',)
                no_candidates = not candidate_codes

                recommendations = []
                scanned = 0
                for code in candidate_codes:
                    if scanned >= universe_limit:
                        break
                    scanned += 1

                    result = await decision_manager(action='analyze', code=code)
                    if not result.get('success'):
                        continue

                    data = result['data']
                    if data['total_score'] >= min_score:
                        recommendations.append({
                            'code': code,
                            'decision': data['decision'],
                            'score': data['total_score'],
                            'reason': data['reason']
                        })
                        if len(recommendations) >= limit:
                            break

                recommendations.sort(key=lambda x: x['score'], reverse=True)

                return _ok({
                    'recommendations': recommendations,
                    'count': len(recommendations),
                    'criteria': criteria,
                    'message': '未找到符合条件的候选股票' if no_candidates else '',
                    'universe': {
                        'candidate_count': len(candidate_codes),
                        'scanned_count': scanned,
                        'limit': limit,
                        'universe_limit': universe_limit,
                        'sector_filter': list(sector_set),
                        'min_market_cap': min_market_cap,
                        'fallback_used': fallback_used,
                        'no_candidates': no_candidates,
                        'source_method': source_method,
                        'filter_chain': filter_chain,
                        'coverage_rate': round(len(recommendations) / max(1, scanned), 4),
                    },
                })
            
            elif action == 'portfolio_advice':
                portfolio_id = kwargs.get('portfolio_id')
                codes = kwargs.get('codes') or []
                weights = kwargs.get('weights') or []

                if isinstance(codes, str):
                    codes = [c.strip() for c in codes.split(',') if c.strip()]
                if isinstance(weights, str):
                    weights = [w.strip() for w in weights.split(',') if str(w).strip()]

                holdings = []
                if codes:
                    if weights and len(weights) != len(codes):
                        return _fail('weights 数量必须与 codes 一致')
                    holdings = [
                        {
                            'code': normalize_code(code),
                            'weight': float(weights[idx]) if weights else 1.0 / len(codes),
                        }
                        for idx, code in enumerate(codes)
                        if normalize_code(code)
                    ]
                elif portfolio_id:
                    async with db.acquire() as conn:
                        holdings = await conn.fetch(
                            "SELECT * FROM holdings WHERE portfolio_id = $1",
                            portfolio_id
                        )
                else:
                    return _fail('需要提供 portfolio_id 或 codes + weights')

                if not holdings:
                    return _ok({'message': '当前组合无持仓，请先添加持仓后再操作', 'portfolio_id': portfolio_id, 'codes': codes})
                
                advice_list = []
                
                for holding in holdings:
                    code = holding['code']
                    
                    result = await decision_manager(action='analyze', code=code)
                    
                    if result.get('success'):
                        data = result['data']
                        advice_list.append({
                            'code': code,
                            'decision': data['decision'],
                            'score': data['total_score'],
                            'action': '建议加仓' if data['decision'] in ['strong_buy', 'buy'] else (
                                '建议减仓' if data['decision'] in ['sell', 'strong_sell'] else '建议持有'
                            )
                        })

                if not advice_list:
                    return _fail('组合中的股票均未能完成分析，无法生成组合建议')
                
                avg_score = np.mean([a['score'] for a in advice_list])
                
                if avg_score >= 65:
                    overall_advice = '组合整体表现良好，可继续持有'
                elif avg_score >= 50:
                    overall_advice = '组合表现中性，建议优化持仓结构'
                else:
                    overall_advice = '组合表现较弱，建议调整持仓'
                
                return _ok({
                    'portfolio_id': portfolio_id,
                    'codes': [h['code'] for h in holdings],
                    'overall_score': float(avg_score),
                    'overall_advice': overall_advice,
                    'holdings_advice': advice_list
                })
            
            else:
                return _fail(f'Unknown action: {action}. Supported: help, analyze, recommend, portfolio_advice')
        except Exception as e:
            msg = str(e).strip()
            if not msg:
                msg = f'{type(e).__name__}: decision_manager({action}) 内部异常'
            return fail_with_meta(
                msg,
                tool_name='decision_manager',
                action=action,
                started_at=start_time,
                source_chain=['decision_manager'],
                extra_meta=common_meta if 'common_meta' in locals() else None,
            )
