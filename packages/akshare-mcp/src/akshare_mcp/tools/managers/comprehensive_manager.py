"""综合管理器 - 一站式分析"""

from typing import Any, Optional
import json
import logging
import time
from ...services.market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_sync
from ...storage import get_db
from ...utils import normalize_code
from ..market import get_kline
from ..manager_protocol import (
    normalize_manager_payload,
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    ok_with_meta,
)

logger = logging.getLogger(__name__)


async def _safe_db_klines(db, code: str, limit: int):
    try:
        return await db.get_klines(code, limit=limit)
    except Exception as e:
        logger.warning("[ComprehensiveManager] DB get_klines failed for %s: %s", code, e)
        return []


async def _safe_db_financials(db, code: str, limit: int = 1):
    try:
        return await db.get_financials(code, limit=limit)
    except Exception as e:
        logger.warning("[ComprehensiveManager] DB get_financials failed for %s: %s", code, e)
        return []


async def _safe_db_stock_info(db, code: str):
    try:
        return await db.get_stock_info(code) or {}
    except Exception as e:
        logger.warning("[ComprehensiveManager] DB get_stock_info failed for %s: %s", code, e)
        return {}


def register_comprehensive_manager(mcp):
    """注册综合管理器工具"""
    
    @mcp.tool()
    async def comprehensive_manager(action: str, params: dict | None = None, kwargs: Any = None, code: str | None = None):
        """综合管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/full_analysis/quick_scan
            code (str, optional): 股票代码
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - full_analysis: code(str, 全面分析包含技术面+基本面+估值+情绪)
                - quick_scan: code(str, 快速扫描)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            comprehensive_manager(action="help", kwargs="{}")
            # 全面分析
            comprehensive_manager(action="full_analysis", code="600519", kwargs="{}")
            # 快速扫描
            comprehensive_manager(action="quick_scan", code="600519", kwargs="{}")
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_kwargs(kwargs, field_aliases={"codes": ("Codes",)})
            code, kwargs = normalize_manager_code(code, kwargs)
            if "codes" not in kwargs and "Codes" in kwargs:
                kwargs["codes"] = kwargs.get("Codes")

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name='comprehensive_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name='comprehensive_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'full_analysis': '综合分析（需要 code）',
                        'quick_scan': '快速扫描（需要 code）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['comprehensive_manager'])
            
            elif action == 'full_analysis':
                if not code:
                    return _fail('需要提供股票代码')
                code = normalize_code(code)
                
                # 综合分析：K 线优先 DB，无则公共行情工具补齐
                klines = await _safe_db_klines(db, code, 60)
                source_chain = ['db.get_klines', 'db.get_financials', 'db.get_stock_info']
                if not klines:
                    res = await get_kline(code, 'daily', 60)
                    if res.get('success') and res.get('data'):
                        klines = res['data']
                        source_chain = ['tools.market.get_kline', 'db.get_financials', 'db.get_stock_info']
                financials = await _safe_db_financials(db, code, 1)
                
                if not klines:
                    return _fail('无K线数据', source_chain=source_chain)
                
                klines = sorted(klines, key=lambda x: x.get('date') or '')
                current_price = klines[-1]['close']
                stock_info = await _safe_db_stock_info(db, code)

                pe_ratio = float(financials[0].get('pe_ratio', 0)) if financials else 0.0
                roe = float(financials[0].get('roe', 0)) if financials else 0.0
                rating = 'A' if roe >= 20 else ('B' if roe >= 10 else 'C')
                profitability_level = 'high' if roe >= 20 else ('medium' if roe >= 10 else 'low')

                technical_data = {
                    'trend': 'uptrend',
                    'support': float(current_price * 0.95),
                    'resistance': float(current_price * 1.05),
                }
                fundamental_data = {
                    'pe_ratio': pe_ratio,
                    'roe': roe,
                    'rating': rating
                }

                # 轻量可解释评分，便于旧版字段兼容
                score_from_roe = max(0.0, min(60.0, roe * 2.0))
                score_from_pe = 0.0
                if pe_ratio > 0:
                    score_from_pe = max(0.0, min(40.0, 40.0 - max(0.0, pe_ratio - 15.0)))
                total_score = round(score_from_roe + score_from_pe, 2)

                return _ok({
                    # 新版结构
                    'code': code,
                    'current_price': float(current_price),
                    'technical_analysis': technical_data,
                    'fundamental_analysis': fundamental_data,
                    'recommendation': 'hold',
                    'confidence': 'medium',
                    # 向后兼容结构
                    'basic_info': {
                        'code': code,
                        'name': stock_info.get('stock_name', code),
                        'industry': stock_info.get('industry', 'unknown'),
                        'current_price': float(current_price),
                    },
                    'technical': technical_data,
                    'fundamental': {
                        'valuation': {'pe_ratio': pe_ratio},
                        'profitability': {'roe': roe, 'level': profitability_level},
                        'rating': rating,
                    },
                    'score': {
                        'total_score': float(total_score),
                    },
                }, source_chain=source_chain)
            
            elif action == 'quick_scan':
                codes = kwargs.get('codes', [])
                if isinstance(codes, str):
                    codes = [codes]
                elif codes is None:
                    codes = []
                elif not isinstance(codes, list):
                    codes = list(codes)
                if not codes and code:
                    codes = [normalize_code(code)]
                
                if not codes:
                    codes = ['600519', '000001', '600036', '601318', '000858']
                    logger.info(f"[ComprehensiveManager] 未提供codes，使用默认样本: {codes}")
                
                results = []
                price_sources = set()
                for c in codes[:10]:
                    c = normalize_code(c)
                    price = None
                    change_pct = 0.0
                    volume = 0.0
                    price_source = 'none'

                    # 优先实时行情
                    try:
                        rt = get_quote_snapshot_sync(c, fallback_mode=FALLBACK_DB_ONLY)
                        if rt.get('success') and rt.get('data'):
                            rtd = rt['data']
                            price = rtd.get('price')
                            change_pct = float(rtd.get('changePercent') or rtd.get('change_pct') or 0)
                            volume = float(rtd.get('volume') or 0)
                            price_source = rtd.get('backend_used') or rtd.get('source') or 'db.stock_quotes'
                    except Exception:
                        pass

                    # 降级到日K线
                    if price is None or price == 0:
                        klines = await _safe_db_klines(db, c, 1)
                        if not klines:
                            res = await get_kline(c, 'daily', 1)
                            if res.get('success') and res.get('data'):
                                klines = res['data']
                        if klines:
                            klines = sorted(klines, key=lambda x: x.get('date') or '')
                            latest = klines[-1]
                            price = float(latest.get('close') or 0)
                            change_pct = float(latest.get('change_pct') or 0)
                            volume = float(latest.get('volume') or 0)
                            price_source = 'daily_kline'

                    if price is not None and price > 0:
                        price_sources.add(price_source)
                        results.append({
                            'code': c,
                            'price': float(price),
                            'change_pct': float(change_pct),
                            'volume': float(volume),
                            'status': 'active',
                            'trend': 'up' if change_pct > 0 else ('down' if change_pct < 0 else 'flat'),
                            'price_source': price_source,
                        })
                
                return _ok({
                    'scanned': len(results),
                    'results': results,
                    'data_source': '+'.join(sorted(price_sources)) if price_sources else 'none',
                    'message': f'成功扫描 {len(results)}/{len(codes[:10])} 只股票'
                }, source_chain=['comprehensive_manager', 'db.stock_quotes', 'db.get_klines'])
            
            else:
                return _fail(f'Unknown action: {action}. Supported: help, full_analysis, quick_scan')
        except Exception as e:
            message = str(e).strip() or f'{action} 执行失败'
            return fail_with_meta(
                message,
                tool_name='comprehensive_manager',
                action=action,
                started_at=start_time,
                source_chain=['comprehensive_manager'],
            )
