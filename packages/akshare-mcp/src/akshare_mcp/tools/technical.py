"""技术分析工具"""

import time
from typing import List, Optional
from ..services import technical_analysis, pattern_recognition
from ..storage import get_db
from .manager_protocol import fail_with_meta, ok_with_meta


def register(mcp):
    """注册技术分析工具"""

    def _extra_meta(
        *,
        status: str,
        target: str,
        degraded: bool = False,
        extra_quality: dict | None = None,
    ) -> dict:
        quality = {"status": status}
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
            "degraded": degraded,
        }
    
    @mcp.tool()
    async def calculate_technical_indicators(
        code: str,
        indicators: List[str],
        period: str = 'daily',
        limit: int = 250
    ):
        """
        计算技术指标
        
        Args:
            code: 股票代码
            indicators: 指标列表 ['MA', 'EMA', 'RSI', 'MACD', 'KDJ', 'BOLL', 'ATR']
            period: K线周期
            limit: K线数量 (默认250，确保MACD等指标有足够数据)
        """
        started_at = time.perf_counter()
        source_chain = ["technical.calculate_technical_indicators", "db.get_klines"]
        try:
            db = get_db()
            klines = await db.get_klines(code, limit=limit)
            fallback_used = False
            
            # DB 无数据时降级到 API
            if not klines:
                from .market.kline import get_kline
                api_result = await get_kline(code, period, limit)
                if api_result.get("success") and api_result.get("data"):
                    klines = api_result["data"]
                    fallback_used = True
                    source_chain.append("market.get_kline")
            
            if not klines:
                return fail_with_meta(
                    'No kline data found',
                    tool_name="calculate_technical_indicators",
                    action="calculate",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_extra_meta(
                        status="not_found",
                        target=code,
                        degraded=True,
                        extra_quality={"indicator_count": len(indicators or [])},
                    ),
                )
            
            # 检查MACD所需的最小数据量
            if 'MACD' in indicators and len(klines) < 35:
                return fail_with_meta(
                    f'MACD需要至少35天数据，当前只有{len(klines)}天。请增加limit参数或检查数据源。',
                    tool_name="calculate_technical_indicators",
                    action="calculate",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_extra_meta(
                        status="insufficient_data",
                        target=code,
                        degraded=True,
                        extra_quality={"indicator_count": len(indicators or []), "kline_count": len(klines)},
                    ),
                )
            
            results = technical_analysis.calculate_all_indicators(klines, indicators)
            
            return ok_with_meta(
                results,
                tool_name="calculate_technical_indicators",
                action="calculate",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_extra_meta(
                    status="available",
                    target=code,
                    degraded=fallback_used,
                    extra_quality={
                        "indicator_count": len(indicators or []),
                        "kline_count": len(klines),
                        "fallback_used": fallback_used,
                    },
                ),
            )
        
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="calculate_technical_indicators",
                action="calculate",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_extra_meta(status="failed", target=code, degraded=True),
            )
    
    @mcp.tool()
    async def check_candlestick_patterns(
        code: str,
        period: str = 'daily',
        limit: int = 100
    ):
        """
        检测K线形态
        
        Args:
            code: 股票代码
            period: K线周期
            limit: K线数量
        """
        started_at = time.perf_counter()
        source_chain = ["technical.check_candlestick_patterns", "db.get_klines"]
        try:
            db = get_db()
            klines = await db.get_klines(code, limit=limit)
            fallback_used = False
            
            # DB 无数据时降级到 API
            if not klines:
                from .market.kline import get_kline
                api_result = await get_kline(code, period, limit)
                if api_result.get("success") and api_result.get("data"):
                    klines = api_result["data"]
                    fallback_used = True
                    source_chain.append("market.get_kline")
            
            if not klines:
                return fail_with_meta(
                    'No kline data found',
                    tool_name="check_candlestick_patterns",
                    action="detect",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_extra_meta(status="not_found", target=code, degraded=True),
                )
            
            patterns = pattern_recognition.detect_patterns(klines)
            
            return ok_with_meta(
                {'patterns': patterns},
                tool_name="check_candlestick_patterns",
                action="detect",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_extra_meta(
                    status="available",
                    target=code,
                    degraded=fallback_used,
                    extra_quality={
                        "pattern_count": len(patterns or []),
                        "kline_count": len(klines),
                        "fallback_used": fallback_used,
                    },
                ),
            )
        
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="check_candlestick_patterns",
                action="detect",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_extra_meta(status="failed", target=code, degraded=True),
            )
    
    @mcp.tool()
    def get_available_patterns():
        """获取支持的K线形态列表"""
        started_at = time.perf_counter()
        try:
            patterns = pattern_recognition.get_available_patterns()
            return ok_with_meta(
                {'patterns': patterns},
                tool_name="get_available_patterns",
                action="list",
                started_at=started_at,
                source_chain=["technical.get_available_patterns", "pattern_recognition"],
                extra_meta=_extra_meta(
                    status="available",
                    target="pattern_catalog",
                    extra_quality={"pattern_count": len(patterns or [])},
                ),
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="get_available_patterns",
                action="list",
                started_at=started_at,
                source_chain=["technical.get_available_patterns", "pattern_recognition"],
                extra_meta=_extra_meta(status="failed", target="pattern_catalog", degraded=True),
            )
