"""板块管理器 - 板块轮动、板块分析"""

from typing import Any
import time
import numpy as np
from ...storage import get_db
from ..manager_protocol import fail_with_meta, normalize_manager_kwargs, normalize_manager_payload, ok_with_meta

_SECTOR_ALIAS_HINTS = {
    "白酒": ["酿酒", "酒"],
    "酿酒": ["白酒", "酒"],
    "券商": ["证券", "金融"],
    "银行": ["金融"],
    "保险": ["金融"],
}


def _normalize_kwargs(kwargs: dict) -> dict:
    """统一解析 kwargs 参数（兼容 JSON 字符串和 dict）"""
    kwargs = normalize_manager_kwargs(kwargs)
    if "sector" not in kwargs:
        kwargs["sector"] = kwargs.get("block_name") or kwargs.get("sector_name") or kwargs.get("industry")
    if "block_code" not in kwargs:
        kwargs["block_code"] = kwargs.get("sector_code") or kwargs.get("code")
    return kwargs


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


def _normalize_period(kwargs: dict, default: int = 20) -> int:
    raw = kwargs.get('period', kwargs.get('days', default))
    try:
        period = int(raw)
    except Exception:
        period = default
    return max(1, period)


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


def register_sector_manager(mcp):
    """注册板块管理器工具"""
    
    @mcp.tool()
    async def sector_manager(action: str, params: dict | None = None, kwargs: Any = None, sector: str | None = None, block_code: str | None = None, block_type: str | None = None, sectors: list[str] | None = None, period: int | None = None, days: int | None = None):
        """板块管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/list_sectors/sector_performance/sector_rotation/sector_correlation
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - list_sectors: block_type(str, optional, "industry"/"concept"/"region")
                - sector_performance: sector(str, optional), days(int, optional)
                - sector_rotation: days(int, optional)
                - sector_correlation: sectors(list[str], optional)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            sector_manager(action="help", kwargs="{}")
            # 列出行业板块
            sector_manager(action="list_sectors", kwargs='{"block_type":"industry"}')
            # 板块轮动分析
            sector_manager(action="sector_rotation", kwargs='{"days":30}')
            # 板块相关性分析
            sector_manager(action="sector_correlation", kwargs="{}")
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "sector": sector,
                    "block_code": block_code,
                    "block_type": block_type,
                    "sectors": sectors,
                    "period": period,
                    "days": days,
                },
            )
            kwargs = _normalize_kwargs(kwargs)

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="sector_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="sector_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'list_sectors': '列出板块列表',
                        'sector_performance': '板块表现分析（需要 sector/block_code）',
                        'sector_rotation': '板块轮动分析',
                        'sector_correlation': '板块相关性分析',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['sector_manager'])
            
            elif action == 'list_sectors':
                source_chain = ['sector_manager']
                block_type = str(kwargs.get('block_type') or kwargs.get('type') or 'industry').strip().lower()
                if block_type not in ('industry', 'concept', 'region'):
                    return _fail(
                        f'Invalid block_type: {block_type}. Supported: industry, concept, region',
                        source_chain=source_chain,
                    )

                sectors = []
                source = 'db'
                fallback_reason = None
                try:
                    async with db.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT DISTINCT block_code, block_name, block_type FROM market_blocks WHERE block_type = $1 ORDER BY block_name",
                            block_type
                        )
                        sectors = [dict(row) for row in rows]
                        if sectors:
                            source_chain.append('db.market_blocks')
                except Exception as exc:
                    fallback_reason = f'DB读取失败: {exc}'

                if not sectors:
                    from ..market_blocks import get_market_blocks

                    blocks_res = await get_market_blocks(block_type=block_type, limit=200)
                    if not blocks_res.get('success'):
                        if fallback_reason:
                            return _fail(
                                f'{fallback_reason}; {blocks_res.get("error")}',
                                source_chain=_dedupe_chain(source_chain + ['market_blocks.get_market_blocks']),
                            )
                        return _fail(
                            blocks_res.get('error') or '未获取到板块数据',
                            source_chain=_dedupe_chain(source_chain + ['market_blocks.get_market_blocks']),
                        )

                    source = blocks_res.get('data', {}).get('source') or 'fallback'
                    if not fallback_reason:
                        fallback_reason = blocks_res.get('data', {}).get('fallback_reason')
                    source_chain.append('market_blocks.get_market_blocks')
                    sectors = [
                        {
                            'block_code': item.get('code') or item.get('blockCode') or item.get('block_code'),
                            'block_name': item.get('name') or item.get('blockName') or item.get('block_name'),
                            'block_type': block_type,
                        }
                        for item in blocks_res.get('data', {}).get('blocks', [])
                        if item.get('code') or item.get('blockCode') or item.get('block_code')
                    ]

                return _ok({
                    'sectors': sectors,
                    'count': len(sectors),
                    'block_type': block_type,
                    'source': source,
                    'fallback_reason': fallback_reason,
                }, source_chain=_dedupe_chain(source_chain))
            
            elif action == 'sector_performance':
                source_chain = ['sector_manager']
                period = _normalize_period(kwargs, 20)
                requested_sector = str(kwargs.get('sector') or '').strip()
                requested_block_code = str(kwargs.get('block_code') or '').strip()
                explicit_type = kwargs.get('type') or kwargs.get('block_type')
                sector_type = str(explicit_type or 'industry').strip().lower()
                search_types = [sector_type] if explicit_type else ['industry', 'concept', 'region']
                sector_performance = []
                source = 'db'
                fallback_reason = None

                from ..market_blocks import get_market_blocks, get_block_stocks

                async def _load_sector_candidates() -> tuple[list[dict], str, str | None]:
                    local_source = 'db'
                    local_reason = None
                    candidates: list[dict] = []

                    if requested_sector or requested_block_code:
                        best_match = None
                        best_score = -1
                        best_source = local_source
                        for block_type in search_types:
                            blocks_res = await get_market_blocks(block_type=block_type, limit=500)
                            source_chain.append('market_blocks.get_market_blocks')
                            if not blocks_res.get('success'):
                                if not local_reason:
                                    local_reason = blocks_res.get('error')
                                continue
                            current_source = blocks_res.get('data', {}).get('source') or local_source
                            block_items = blocks_res.get('data', {}).get('blocks', [])
                            for block in block_items:
                                block_code = str(block.get('code') or block.get('blockCode') or block.get('block_code') or '').strip()
                                block_name = str(block.get('name') or block.get('blockName') or block.get('block_name') or '').strip()
                                if requested_block_code and block_code == requested_block_code:
                                    return ([{
                                        'block_code': block_code,
                                        'block_name': block_name,
                                        'block_type': block_type,
                                        'avg_change_pct': block.get('avgChange') or block.get('avgChangePct') or block.get('avg_change_pct'),
                                    }], current_source, local_reason)
                                score = _sector_match_score(requested_sector, block_name)
                                if score > best_score:
                                    best_score = score
                                    best_source = current_source
                                    best_match = {
                                        'block_code': block_code,
                                        'block_name': block_name,
                                        'block_type': block_type,
                                        'avg_change_pct': block.get('avgChange') or block.get('avgChangePct') or block.get('avg_change_pct'),
                                    }
                        if best_match and best_score > 0:
                            return [best_match], best_source, local_reason
                        return [], local_source, local_reason

                    try:
                        async with db.acquire() as conn:
                            rows = await conn.fetch(
                                "SELECT block_code, block_name, block_type FROM market_blocks WHERE block_type = $1 LIMIT 20",
                                sector_type
                            )
                            candidates = [dict(row) for row in rows]
                            if candidates:
                                source_chain.append('db.market_blocks')
                    except Exception as exc:
                        local_reason = f'DB读取失败: {exc}'
                        candidates = []

                    if candidates:
                        return candidates, local_source, local_reason

                    blocks_res = await get_market_blocks(block_type=sector_type, limit=20)
                    source_chain.append('market_blocks.get_market_blocks')
                    if not blocks_res.get('success'):
                        local_reason = blocks_res.get('error') or local_reason
                        return [], local_source, local_reason

                    local_source = blocks_res.get('data', {}).get('source') or 'fallback'
                    if not local_reason:
                        local_reason = blocks_res.get('data', {}).get('fallback_reason')
                    candidates = [
                        {
                            'block_code': b.get('code') or b.get('blockCode') or b.get('block_code'),
                            'block_name': b.get('name') or b.get('blockName') or b.get('block_name'),
                            'block_type': sector_type,
                            'avg_change_pct': b.get('avgChange') or b.get('avgChangePct') or b.get('avg_change_pct'),
                        }
                        for b in blocks_res.get('data', {}).get('blocks', [])[:20]
                        if b.get('code') or b.get('blockCode') or b.get('block_code')
                    ]
                    return candidates, local_source, local_reason

                async def _load_stocks_for_sector(block_code: str) -> tuple[list[dict], str]:
                    try:
                        async with db.acquire() as conn:
                            rows = await conn.fetch(
                                "SELECT stock_code FROM block_stocks WHERE block_code = $1",
                                block_code
                            )
                        if rows:
                            source_chain.append('db.block_stocks')
                            return [{'code': str(row['stock_code'])} for row in rows if row.get('stock_code')], 'db_block_stocks'
                    except Exception:
                        pass

                    stocks_res = await get_block_stocks(block_code)
                    source_chain.append('market_blocks.get_block_stocks')
                    if stocks_res.get('success'):
                        items = stocks_res.get('data', {}).get('stocks', [])
                        return [
                            {
                                'code': item.get('code') or item.get('stock_code'),
                                'price': item.get('price'),
                                'change_pct': item.get('changePercent') or item.get('change_pct'),
                            }
                            for item in items
                            if item.get('code') or item.get('stock_code')
                        ], stocks_res.get('data', {}).get('source') or 'block_api'
                    return [], 'none'

                sectors, source, fallback_reason = await _load_sector_candidates()
                sector_performance = []
                
                for sector in sectors:
                    block_code = sector['block_code']
                    block_name = sector['block_name']
                    stocks, stock_source = await _load_stocks_for_sector(block_code)
                    if not stocks:
                        continue
                    realtime_returns = []
                    for stock in stocks[:20]:
                        raw_change = stock.get('change_pct')
                        try:
                            if raw_change is not None:
                                realtime_returns.append(float(raw_change) / 100.0)
                        except Exception:
                            pass

                    avg_return = None
                    valid_count = 0
                    perf_source = stock_source
                    if realtime_returns:
                        avg_return = sum(realtime_returns) / len(realtime_returns)
                        valid_count = len(realtime_returns)
                    else:
                        total_return = 0.0
                        for stock in stocks[:10]:
                            code = stock.get('code') or stock.get('stock_code')
                            if not code:
                                continue
                            try:
                                klines = await db.get_klines(code, limit=period + 1)
                                if klines:
                                    source_chain.append('db.get_klines')
                            except Exception:
                                klines = []

                            if len(klines) >= 2:
                                start_price = klines[0]['close']
                                end_price = klines[-1]['close']
                                if start_price:
                                    stock_return = (end_price - start_price) / start_price
                                    total_return += stock_return
                                    valid_count += 1
                        if valid_count > 0:
                            avg_return = total_return / valid_count
                            perf_source = 'db_kline'

                    if avg_return is None:
                        try:
                            avg_change_pct = float(sector.get('avg_change_pct') or 0)
                        except Exception:
                            avg_change_pct = 0.0
                        if avg_change_pct:
                            avg_return = avg_change_pct / 100.0
                            valid_count = 0
                            perf_source = 'block_summary'

                    if avg_return is not None:
                        sector_performance.append({
                            'blockCode': block_code,
                            'blockName': block_name,
                            'return': float(avg_return),
                            'returnPct': f"{avg_return*100:.2f}%",
                            'stocksCount': valid_count if valid_count > 0 else len(stocks),
                            'strength': 'strong' if avg_return > 0.1 else ('weak' if avg_return < -0.05 else 'medium'),
                            'source': perf_source,
                        })
                
                sector_performance.sort(key=lambda x: x['return'], reverse=True)
                
                return _ok({
                    'period': period,
                    'sectors': sector_performance,
                    'top_sectors': sector_performance[:5],
                    'bottom_sectors': sector_performance[-5:] if len(sector_performance) > 5 else [],
                    'source': source,
                    'fallback_reason': fallback_reason,
                }, source_chain=_dedupe_chain(source_chain))
            
            elif action == 'sector_rotation':
                source_chain = ['sector_manager']
                period = _normalize_period(kwargs, 20)
                
                # P3-5.9 fix: 同源化 - 显式记录 sector_performance 上游(诊断报告 §5.9)
                # 历史问题:sector_rotation 的数据源标识与 sector_performance 不一致,AI 难以追溯
                # 修复:把 sector_performance 的 source 透传到 sector_rotation 输出
                performance_result = await sector_manager(
                    action='sector_performance',
                    params={'period': period}
                )
                source_chain.extend(performance_result.get('meta', {}).get('source_chain') or [])
                upstream_source = (performance_result.get('data') or {}).get('source') or 'unknown'
                upstream_fallback_reason = (performance_result.get('data') or {}).get('fallback_reason')
                
                sectors = []
                if performance_result.get('success') and performance_result.get('data'):
                    sectors = performance_result['data'].get('sectors', [])
                
                # 如果 sector_performance 返回空，直接用 get_market_blocks 的涨跌幅数据
                rotation_used_fallback = False
                if not sectors:
                    rotation_used_fallback = True
                    try:
                        from ..market_blocks import get_market_blocks
                        blocks_res = await get_market_blocks(block_type='industry', limit=20)
                        source_chain.append('market_blocks.get_market_blocks')
                        if blocks_res.get('success') and blocks_res.get('data', {}).get('blocks'):
                            for b in blocks_res['data']['blocks']:
                                avg_chg = float(
                                    b.get('avgChange')
                                    or b.get('avgChangePct')
                                    or b.get('avg_change_pct')
                                    or 0
                                )
                                sectors.append({
                                    'blockCode': b.get('blockCode') or b.get('code') or b.get('block_code', ''),
                                    'blockName': b.get('blockName') or b.get('name') or b.get('block_name', ''),
                                    'return': avg_chg / 100.0,
                                    'returnPct': f"{avg_chg:.2f}%",
                                    'stocksCount': b.get('stockCount') or b.get('stock_count') or 0,
                                    'strength': 'strong' if avg_chg > 2 else ('weak' if avg_chg < -1 else 'medium')
                                })
                            sectors.sort(key=lambda x: x['return'], reverse=True)
                    except Exception:
                        pass
                
                if not sectors:
                    return _ok({
                        'period': period,
                        'message': '无板块数据，请先获取板块信息',
                        'strong_sectors': [],
                        'weak_sectors': [],
                        'rotation_advice': [],
                        'market_style': 'unknown',
                        'upstream_source': upstream_source,
                    }, source_chain=_dedupe_chain(source_chain))
                
                total_count = len(sectors)
                strong_count = max(1, int(total_count * 0.3))
                weak_count = max(1, int(total_count * 0.3))
                
                strong_sectors = sectors[:strong_count]
                weak_sectors = sectors[-weak_count:]
                
                rotation_advice = []
                
                for sector in strong_sectors:
                    rotation_advice.append({
                        'sector': sector.get('blockName') or sector.get('block_name', ''),
                        'action': 'overweight',
                        'reason': f"板块表现强势，{period}日涨幅{sector.get('returnPct') or sector.get('return_pct', '')}"
                    })

                for sector in weak_sectors:
                    rotation_advice.append({
                        'sector': sector.get('blockName') or sector.get('block_name', ''),
                        'action': 'underweight',
                        'reason': f"板块表现弱势，{period}日涨幅{sector.get('returnPct') or sector.get('return_pct', '')}"
                    })
                
                top_return = strong_sectors[0]['return']
                return _ok({
                    'period': period,
                    'strong_sectors': strong_sectors,
                    'weak_sectors': weak_sectors,
                    'rotation_advice': rotation_advice,
                    'market_style': 'growth' if top_return > 0.15 else (
                        'value' if top_return < 0.05 else 'balanced'
                    ),
                    # P3-5.9 fix: 透出上游来源,确保 rotation 与 performance 同源可追溯
                    'upstream_source': upstream_source,
                    'upstream_fallback_reason': upstream_fallback_reason,
                    'used_market_blocks_fallback': rotation_used_fallback,
                }, source_chain=_dedupe_chain(source_chain))
            
            elif action == 'sector_correlation':
                source_chain = ['sector_manager']
                sectors = kwargs.get('sectors', [])
                period = kwargs.get('period', 60)
                
                if len(sectors) < 2:
                    return _fail('需要至少2个板块代码', source_chain=source_chain)
                
                sector_returns = {}
                
                for sector_code in sectors:
                    async with db.acquire() as conn:
                        stocks = await conn.fetch(
                            "SELECT stock_code FROM block_stocks WHERE block_code = $1 LIMIT 5",
                            sector_code
                        )
                    source_chain.append('db.block_stocks')
                    
                    if not stocks:
                        continue
                    
                    daily_returns = []
                    
                    for i in range(period):
                        day_return = 0
                        valid_count = 0
                        
                        for stock in stocks:
                            code = stock['stock_code']
                            klines = await db.get_klines(code, limit=period + 1)
                            if klines:
                                source_chain.append('db.get_klines')
                            
                            if len(klines) > i + 1:
                                ret = (klines[-(i+1)]['close'] - klines[-(i+2)]['close']) / klines[-(i+2)]['close']
                                day_return += ret
                                valid_count += 1
                        
                        if valid_count > 0:
                            daily_returns.append(day_return / valid_count)
                    
                    sector_returns[sector_code] = daily_returns
                
                correlation_matrix = {}
                
                for sector1 in sectors:
                    if sector1 not in sector_returns:
                        continue
                    
                    correlation_matrix[sector1] = {}
                    
                    for sector2 in sectors:
                        if sector2 not in sector_returns:
                            continue
                        
                        if len(sector_returns[sector1]) > 0 and len(sector_returns[sector2]) > 0:
                            min_len = min(len(sector_returns[sector1]), len(sector_returns[sector2]))
                            corr = np.corrcoef(
                                sector_returns[sector1][:min_len],
                                sector_returns[sector2][:min_len]
                            )[0, 1]
                            correlation_matrix[sector1][sector2] = float(corr)
                        else:
                            correlation_matrix[sector1][sector2] = 0.0
                
                # P3-5.7 fix: 全空时显式 fail(诊断报告 §5.7)
                # 历史问题:5 个 sectors 输入但 correlation_matrix={},success=true 不显式失败
                if len(correlation_matrix) == 0:
                    return _fail(
                        f'所有 {len(sectors)} 个板块均无可用 K 线数据,无法计算相关性',
                        source_chain=_dedupe_chain(source_chain),
                    )

                return _ok({
                    'sectors': sectors,
                    'period': period,
                    'correlation_matrix': correlation_matrix,
                    'computed_pairs': sum(len(v) for v in correlation_matrix.values()),
                    'sectors_with_returns': list(sector_returns.keys()),
                    'sectors_missing_returns': [s for s in sectors if s not in sector_returns],
                    # P3-5.7 fix: 部分缺失时显式标 degraded(诊断报告 §5.7)
                    'degraded': len(correlation_matrix) < len(sectors),
                    'fallback_reason': (
                        f'{len(sectors) - len(correlation_matrix)} sectors_returns_unavailable'
                        if len(correlation_matrix) < len(sectors)
                        else None
                    ),
                    'interpretation': {
                        'high_correlation': '>0.7表示高度相关',
                        'low_correlation': '<0.3表示低相关',
                        'negative_correlation': '<0表示负相关'
                    }
                }, source_chain=_dedupe_chain(source_chain))
            
            else:
                return _fail(
                    f'Unknown action: {action}. Supported: help, list_sectors, sector_performance, sector_rotation, sector_correlation',
                    source_chain=['sector_manager'],
                )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name='sector_manager',
                action=action,
                started_at=start_time,
                source_chain=['sector_manager'],
            )
