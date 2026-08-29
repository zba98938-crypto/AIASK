"""技术分析管理器 - 连接真实服务"""

import json
import logging
import time
from datetime import datetime
from typing import Optional, List, Any

from ...storage import get_db
from ...data_source import data_source
from ...utils import normalize_code
from ..manager_protocol import (
    extract_common_meta,
    fail_with_meta,
    normalize_manager_payload,
    ok_with_meta,
)

logger = logging.getLogger(__name__)


def _ensure_serializable(obj: Any) -> Any:
    """确保返回值可 JSON 序列化，避免 numpy 等类型导致客户端解析错误。"""
    if hasattr(obj, "item"):
        return obj.item()
    if isinstance(obj, dict):
        return {k: _ensure_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_ensure_serializable(x) for x in obj]
    if isinstance(obj, (float, int)) and hasattr(obj, "dtype"):
        return float(obj) if getattr(obj, "dtype", None) else obj
    return obj


def _safe_last_numeric(series: Any) -> float | None:
    """安全获取序列最后一个数值。"""
    if not isinstance(series, list) or not series:
        return None
    val = series[-1]
    try:
        return float(val)
    except Exception:
        return None


def _build_indicator_summary(klines: list[dict], results: dict) -> dict:
    """基于 MA/RSI/MACD/KDJ/BOLL/ATR 生成摘要。"""
    if not klines:
        return {
            'trend': {'label': 'unknown', 'reason': '缺少K线数据'},
            'momentum': {'label': 'unknown'},
            'volatility': {'label': 'unknown'},
            'signals': [],
            'suggestion': 'wait',
            'suggestion_text': '数据不足，建议观望'
        }

    closes = [float(k.get('close', 0) or 0) for k in klines]
    latest_close = closes[-1] if closes else 0.0

    ma20 = _safe_last_numeric(results.get('ma'))
    rsi_val = None
    if isinstance(results.get('rsi'), dict):
        try:
            rsi_val = float(results['rsi'].get('value')) if results['rsi'].get('value') is not None else None
        except Exception:
            rsi_val = None

    macd_hist = None
    if isinstance(results.get('macd'), dict):
        macd_hist = _safe_last_numeric(results['macd'].get('histogram'))

    atr_val = _safe_last_numeric(results.get('atr'))

    boll_upper = boll_middle = boll_lower = None
    if isinstance(results.get('boll'), dict):
        boll_upper = _safe_last_numeric(results['boll'].get('upper'))
        boll_middle = _safe_last_numeric(results['boll'].get('middle'))
        boll_lower = _safe_last_numeric(results['boll'].get('lower'))

    signals = []
    score = 0

    # 趋势
    trend_label = 'neutral'
    trend_reason = '趋势中性'
    if ma20 is not None and latest_close > 0:
        if latest_close > ma20:
            trend_label = 'up'
            trend_reason = '收盘价位于MA20上方'
            signals.append('MA20上方，趋势偏多')
            score += 1
        elif latest_close < ma20:
            trend_label = 'down'
            trend_reason = '收盘价位于MA20下方'
            signals.append('MA20下方，趋势偏空')
            score -= 1

    # 动量
    momentum_label = 'neutral'
    if rsi_val is not None:
        if rsi_val < 30:
            momentum_label = 'oversold'
            signals.append(f'RSI={rsi_val:.1f}，超卖')
            score += 1
        elif rsi_val > 70:
            momentum_label = 'overbought'
            signals.append(f'RSI={rsi_val:.1f}，超买')
            score -= 1
        else:
            momentum_label = 'normal'
            signals.append(f'RSI={rsi_val:.1f}，区间正常')

    if macd_hist is not None:
        if macd_hist > 0:
            signals.append('MACD柱线为正，动量偏强')
            score += 1
        elif macd_hist < 0:
            signals.append('MACD柱线为负，动量偏弱')
            score -= 1

    # 波动 — ATR 纳入评分
    volatility_label = 'normal'
    atr_pct = None
    if atr_val is not None and latest_close > 0:
        atr_pct = atr_val / latest_close
        if atr_pct >= 0.05:
            volatility_label = 'high'
            signals.append(f'ATR/Close={atr_pct:.2%}，波动偏高，风险较大')
            score -= 1
        elif atr_pct >= 0.04:
            volatility_label = 'elevated'
            signals.append(f'ATR/Close={atr_pct:.2%}，波动偏高')
        elif atr_pct <= 0.015:
            volatility_label = 'low'
            signals.append(f'ATR/Close={atr_pct:.2%}，波动较低')

    # 布林带位置 — 纳入评分
    boll_width_pct = None
    if boll_upper is not None and boll_lower is not None and boll_middle:
        try:
            boll_width_pct = (boll_upper - boll_lower) / float(boll_middle)
        except Exception:
            boll_width_pct = None

    if boll_lower is not None and latest_close > 0 and boll_lower > 0:
        if latest_close <= boll_lower * 1.01:
            signals.append(f'触及布林下轨({boll_lower:.1f})，超卖信号')
            score += 1
    if boll_upper is not None and latest_close > 0 and boll_upper > 0:
        if latest_close >= boll_upper * 0.99:
            signals.append(f'触及布林上轨({boll_upper:.1f})，超买信号')
            score -= 1

    if score >= 2:
        suggestion, suggestion_text = 'buy', '技术面偏强，可关注低吸机会'
    elif score == 1:
        suggestion, suggestion_text = 'hold', '技术面小幅偏强，可持有观察'
    elif score == 0:
        suggestion, suggestion_text = 'wait', '技术信号分化，建议观望'
    else:
        suggestion, suggestion_text = 'sell', '技术面偏弱，注意风险控制'

    return {
        'trend': {'label': trend_label, 'reason': trend_reason},
        'momentum': {
            'label': momentum_label,
            'rsi': rsi_val,
            'macd_histogram': macd_hist,
        },
        'volatility': {
            'label': volatility_label,
            'atr_pct': round(atr_pct, 6) if atr_pct is not None else None,
            'boll_width_pct': round(boll_width_pct, 6) if boll_width_pct is not None else None,
        },
        'signals': signals,
        'suggestion': suggestion,
        'suggestion_text': suggestion_text,
    }





def register_technical_analysis_manager(mcp):
    """注册技术分析管理器工具"""

    @mcp.tool()
    async def technical_analysis_manager(
        action: str,
        params: dict | None = None,
        kwargs: Any = None,
        code: str | None = None,
        indicators: Optional[List[str]] = None,
        period: str = 'daily',
        limit: int = 250,
    ):
        """
        技术分析管理器（统一 action + kwargs 协议，连接真实服务）

        Args:
            action (str, required): 操作类型，可选 help/calculate/list_indicators/check_patterns
            code (str, optional): 股票代码，calculate 和 check_patterns 时必填
            indicators (list[str], optional): 指标列表，calculate 时使用，如 ["MA","RSI","MACD","KDJ","BOLL","ATR"]
            period (str, optional): K线周期，默认 "daily"
            limit (int, optional): K线数量，默认 250（MACD 至少需要 35 条数据）
            kwargs: JSON 字符串或关键字参数

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}
            - calculate: 返回 indicators 字典，包含各指标最新值和历史序列
            - list_indicators: 返回支持的指标列表
            - check_patterns: 返回检测到的K线形态列表

        Examples:
            # 查看帮助
            technical_analysis_manager(action="help", kwargs="{}")
            # 计算技术指标
            technical_analysis_manager(action="calculate", code="600519", indicators=["MA","RSI","MACD"], kwargs="{}")
            # 检测K线形态
            technical_analysis_manager(action="check_patterns", code="600519", kwargs="{}")
            # 列出支持的指标
            technical_analysis_manager(action="list_indicators", kwargs="{}")
        """
        start_time = time.perf_counter()
        try:
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs, code=code)
            action = (action or '').strip()
            code = code or kwargs.get("code") or kwargs.get("Code") or kwargs.get("stock_code") or kwargs.get("symbol")
            if indicators is None:
                indicators = kwargs.get("indicators")
            period = kwargs.get("period") or period
            limit = kwargs.get("limit", limit)
            if isinstance(limit, str):
                try:
                    limit = int(limit)
                except ValueError:
                    limit = 250
            if isinstance(indicators, str):
                try:
                    indicators = json.loads(indicators) if indicators.strip().startswith("[") else [indicators.strip()]
                except Exception:
                    indicators = ["MA", "RSI", "MACD"]

            # 统一可选参数（向后兼容）
            common_meta = extract_common_meta(
                kwargs,
                defaults={
                    'as_of': '',
                    'adjust': '',
                    'price_source_policy': 'auto',
                    'explain': True,
                    'strict_mode': False,
                },
            )
            explain = common_meta['explain']

            def _ok(data: dict, source_chain=None, data_timestamp: str | None = None):
                return ok_with_meta(
                    data,
                    tool_name='technical_analysis_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    data_timestamp=data_timestamp,
                    extra_meta=common_meta,
                )

            def _fail(message: str, source_chain=None, data_timestamp: str | None = None):
                return fail_with_meta(
                    message,
                    tool_name='technical_analysis_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                    data_timestamp=data_timestamp,
                    extra_meta=common_meta,
                )

            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'calculate': '计算技术指标（需要 code, indicators）',
                        'check_patterns': '检测K线形态（需要 code）',
                        'list_indicators': '列出支持的技术指标',
                        'help': '显示帮助信息',
                    }
                })

            elif action == 'calculate':
                if not code:
                    return _fail('需要提供股票代码（code）')

                code = normalize_code(code)
                db = get_db()
                source_chain = ['sqlite']

                # 1. 尝试从DB获取K线
                klines = await db.get_klines(code, limit=limit)

                # 2. DB无数据时自动获取
                if not klines:
                    logger.info(f"[TechnicalAnalysisManager] Fetching klines for {code}")
                    klines = data_source.get_kline(code, period, limit)
                    source_chain = ['data_source']

                    # 保存到DB
                    if klines:
                        try:
                            await db.save_klines(code, klines)
                        except Exception as e:
                            logger.warning(f"[TechnicalAnalysisManager] Failed to save klines: {e}")

                if not klines:
                    return _fail(f'无法获取K线数据，请检查股票代码 {code}', source_chain=source_chain)

                # 3. 计算技术指标
                from ...services.technical_analysis import technical_analysis
                indicators = indicators or ['MA', 'RSI', 'MACD']

                # 检查MACD所需数据量
                if any((str(i).upper() == 'MACD') for i in indicators) and len(klines) < 35:
                    return _fail(f'MACD需要至少35天数据，当前只有{len(klines)}天', source_chain=source_chain)

                klines = sorted(klines, key=lambda x: x.get('date') or '')
                analysis_date = (klines[-1].get('date') if klines else None) or datetime.now().strftime('%Y-%m-%d')
                results = technical_analysis.calculate_all_indicators(klines, indicators)
                summary = _build_indicator_summary(klines, results)

                out = {
                    **results,
                    'summary': summary,
                    'code': code,
                    'data_source': klines[-1].get('source', 'unknown') if klines else 'unknown',
                    'kline_count': len(klines),
                    'analysis_date': analysis_date,
                    'requested_indicators': indicators,
                }
                if explain:
                    out['diagnostic'] = {
                        'trace': [
                            f'code={code}',
                            f'period={period}',
                            f'kline_count={len(klines)}',
                            f'indicators={indicators}',
                            f'summary_suggestion={summary.get("suggestion")}',
                        ]
                    }

                return _ok(
                    _ensure_serializable(out),
                    source_chain=source_chain + ['technical_analysis'],
                    data_timestamp=analysis_date,
                )

            elif action == 'check_patterns':
                if not code:
                    return _fail('需要提供股票代码（code）')

                code = normalize_code(code)
                db = get_db()
                source_chain = ['sqlite']

                # 获取K线
                klines = await db.get_klines(code, limit=100)
                if not klines:
                    klines = data_source.get_kline(code, period, 100)
                    source_chain = ['data_source']
                    if klines:
                        try:
                            await db.save_klines(code, klines)
                        except Exception:
                            pass

                if not klines:
                    return _fail(f'无法获取K线数据，请检查股票代码 {code}', source_chain=source_chain)

                # 检测形态
                from ...services.pattern_recognition import pattern_recognition
                patterns = pattern_recognition.detect_patterns(klines)
                analysis_date = (sorted(klines, key=lambda x: x.get('date') or '')[-1].get('date')) if klines else None

                return _ok(_ensure_serializable({
                    'code': code,
                    'patterns': patterns,
                    'kline_count': len(klines)
                }), source_chain=source_chain + ['pattern_recognition'], data_timestamp=analysis_date)

            elif action == 'list_indicators':
                return _ok({
                    'indicators': ['MA', 'EMA', 'RSI', 'MACD', 'KDJ', 'BOLL', 'ATR'],
                    'descriptions': {
                        'MA': '移动平均线 - 趋势跟踪指标',
                        'EMA': '指数移动平均线 - 对近期价格更敏感',
                        'RSI': '相对强弱指标 - 超买超卖判断',
                        'MACD': '指数平滑异同移动平均线 - 趋势和动量',
                        'KDJ': '随机指标 - 超买超卖和转折点',
                        'BOLL': '布林带 - 波动率和支撑阻力',
                        'ATR': '平均真实波幅 - 波动率测量'
                    }
                })
            else:
                return _fail(f'Unknown action: {action}. Supported: help, calculate, check_patterns, list_indicators')
        except Exception as e:
            logger.error(f"[TechnicalAnalysisManager] Error: {e}")
            return fail_with_meta(
                str(e),
                tool_name='technical_analysis_manager',
                action=action,
                started_at=start_time,
                source_chain=['technical_analysis_manager'],
            )
