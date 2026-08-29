"""
市场板块工具 - 获取板块数据
数据源优先级: DB缓存 → 东方财富datacenter HTTP → AKShare
"""

import asyncio
import json
import urllib.request
from typing import Dict, Any, Optional, List
from ..storage.sqlite import get_db
from ..utils import (
    fail,
    ok,
    parse_numeric,
    propagate_data_quality_to_top,
    safe_float,
    safe_stderr_print,
    suppress_stdout,
)
from ..core.normalize import normalize_block_list, normalize_block_stock_list
from ..provider_contracts import attach_tool_provider_contract_meta

try:
    import akshare as ak
except ImportError:
    ak = None


def _with_provider_contract(result: dict, tool_name: str, **kwargs: Any) -> dict:
    return attach_tool_provider_contract_meta(
        propagate_data_quality_to_top(result),
        tool_name=tool_name,
        **kwargs,
    )

def _fetch_from_sector_spot() -> list:
    """通过 ak.stock_sector_spot() 获取行业板块数据（新浪接口，不受 Clash 代理影响）"""
    if ak is None:
        return []
    try:
        with suppress_stdout("[MarketBlocks] stock_sector_spot"):
            df = ak.stock_sector_spot()
        if df is None or df.empty:
            return []
        blocks = []
        for _, row in df.iterrows():
            label = str(row.get('label', '') or '')
            name = str(row.get('板块', '') or '')
            if not label or not name:
                continue
            blocks.append({
                'block_code': label,
                'block_name': name,
                'block_type': 'industry',
                'stock_count': int(row.get('公司家数', 0) or 0),
                'avg_change_pct': float(row.get('涨跌幅', 0) or 0),
                'total_amount': float(row.get('总成交额', 0) or 0),
                'leader_code': str(row.get('股票代码', '') or '').replace('sh', '').replace('sz', '').replace('bj', ''),
                'leader_name': str(row.get('股票名称', '') or ''),
            })
        blocks.sort(key=lambda b: b['avg_change_pct'], reverse=True)
        return blocks
    except Exception as e:
        safe_stderr_print(f"[MarketBlocks] sector_spot失败: {e}")
        return []


def _fetch_from_ths(block_type: str) -> list:
    """通过同花顺接口获取板块名称列表（无涨跌幅，仅名称/代码）"""
    if ak is None:
        return []
    try:
        if block_type == 'industry':
            with suppress_stdout("[MarketBlocks] stock_board_industry_name_ths"):
                df = ak.stock_board_industry_name_ths()
        elif block_type == 'concept':
            with suppress_stdout("[MarketBlocks] stock_board_concept_name_ths"):
                df = ak.stock_board_concept_name_ths()
        else:
            return []
        if df is None or df.empty:
            return []
        blocks = []
        for _, row in df.iterrows():
            name = str(row.get('name', '') or '')
            code = str(row.get('code', '') or '')
            if not name:
                continue
            blocks.append({
                'block_code': code,
                'block_name': name,
                'block_type': block_type,
                'stock_count': None,
                'avg_change_pct': None,
                'total_amount': None,
                'leader_code': None,
                'leader_name': None,
                'degraded': True,
                'fallback_reason': '同花顺名称列表不含实时统计字段，已降级为代码/名称摘要；如需成分股请调用 get_block_stocks',
            })
        return blocks
    except Exception as e:
        safe_stderr_print(f"[MarketBlocks] 同花顺{block_type}失败: {e}")
        return []


from .block_helpers import (
    _fetch_concept_stocks_from_ths,
    _is_placeholder_summary,
    _sanitize_placeholder_blocks,
)
def _fetch_region_area_summary() -> list:
    """地域板块降级：使用 AKShare 的地区成交汇总接口返回可用地区列表。"""
    if ak is None or not hasattr(ak, "stock_szse_area_summary"):
        return []
    try:
        df = ak.stock_szse_area_summary()
        if df is None or df.empty:
            return []

        blocks = []
        for _, row in df.iterrows():
            name = str(row.get("地区", "") or "").strip()
            if not name:
                continue

            total_amount = safe_float(row.get("总交易额"))
            stock_amount = safe_float(row.get("股票交易额"))
            blocks.append({
                "block_code": f"region::{name}",
                "block_name": name,
                "block_type": "region",
                "stock_count": 0,
                "avg_change_pct": 0.0,
                "total_amount": total_amount if total_amount is not None else stock_amount,
                "leader_code": None,
                "leader_name": None,
                "degraded": True,
                "fallback_reason": "当前环境缺少地域板块接口，已降级为地区成交汇总列表，不含涨跌幅与成分股",
            })

        blocks.sort(key=lambda item: item.get("total_amount") or 0, reverse=True)
        return blocks
    except Exception as e:
        safe_stderr_print(f"[MarketBlocks] 地区汇总降级失败: {e}")
        return []


async def _fetch_from_db(block_type: str, limit: Optional[int]) -> list:
    """从DB读取近期板块数据(30分钟内视为有效)"""
    try:
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT block_code, block_name, block_type, stock_count, "
                "avg_change_pct, total_amount, leader_code, leader_name "
                "FROM market_blocks WHERE block_type = $1 "
                "AND updated_at > datetime(CURRENT_TIMESTAMP, '-30 minutes') "
                "ORDER BY avg_change_pct DESC LIMIT $2",
                block_type, limit or 500
            )
            return [dict(r) for r in rows] if rows else []
    except Exception as e:
        safe_stderr_print(f"[MarketBlocks] DB读取失败: {e}")
        return []


async def _fetch_from_db_any_age(block_type: str, limit: Optional[int]) -> list:
    try:
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT block_code, block_name, block_type, stock_count, "
                "avg_change_pct, total_amount, leader_code, leader_name, updated_at "
                "FROM market_blocks WHERE block_type = $1 "
                "ORDER BY updated_at DESC LIMIT $2",
                block_type,
                limit or 500,
            )
        blocks = []
        for row in rows or []:
            item = dict(row)
            item["degraded"] = True
            item["fallback_reason"] = "using stale db cache because live providers are unavailable"
            blocks.append(item)
        return blocks
    except Exception as e:
        safe_stderr_print(f"[MarketBlocks] stale DB read failed: {e}")
        return []


async def _enrich_blocks_from_cache(blocks: list[dict]) -> list[dict]:
    """用本地缓存补齐 THS 名称列表缺少的统计字段，避免把未知值伪装成 0。"""
    if not blocks:
        return blocks

    block_codes = [
        str(item.get('block_code') or '').strip()
        for item in blocks
        if str(item.get('block_code') or '').strip()
    ]
    if not block_codes:
        return blocks

    try:
        db = get_db()
        async with db.acquire() as conn:
            cached_rows = await conn.fetch(
                """SELECT block_code, stock_count, avg_change_pct, total_amount, leader_code, leader_name
                   FROM (
                       SELECT block_code, stock_count, avg_change_pct, total_amount, leader_code, leader_name,
                              ROW_NUMBER() OVER (PARTITION BY block_code ORDER BY updated_at DESC) AS rn
                       FROM market_blocks
                       WHERE block_code IN ($1)
                   ) ranked
                   WHERE rn = 1""",
                block_codes,
            )
            stock_rows = await conn.fetch(
                """SELECT block_code, COUNT(*) AS stock_count
                   FROM block_stocks
                   WHERE block_code IN ($1)
                   GROUP BY block_code""",
                block_codes,
            )
    except Exception as e:
        safe_stderr_print(f"[MarketBlocks] 缓存补齐失败: {e}")
        return blocks

    meta_by_code = {
        str(row.get('block_code') or ''): dict(row)
        for row in (cached_rows or [])
    }
    stock_count_by_code = {
        str(row.get('block_code') or ''): int(row.get('stock_count') or 0)
        for row in (stock_rows or [])
    }

    enriched = []
    for item in blocks:
        code = str(item.get('block_code') or '').strip()
        cached = meta_by_code.get(code, {})
        merged = dict(item)

        cached_count = stock_count_by_code.get(code)
        if cached_count is not None and cached_count > 0:
            merged['stock_count'] = cached_count
        elif merged.get('stock_count') is None and cached.get('stock_count') is not None:
            merged['stock_count'] = int(cached.get('stock_count') or 0)

        for field in ('avg_change_pct', 'total_amount', 'leader_code', 'leader_name'):
            if merged.get(field) is None and cached.get(field) is not None:
                merged[field] = cached.get(field)

        enriched.append(merged)

    return enriched

def _fetch_from_akshare(block_type: str) -> list:
    """从AKShare获取(最后降级)"""
    if ak is None:
        return []
    try:
        fn = {'industry': ak.stock_board_industry_name_em,
              'concept': ak.stock_board_concept_name_em,
              'region': ak.stock_board_region_name_em}.get(block_type)
        if not fn:
            return []
        df = fn()
        if df is None or df.empty:
            return []
        blocks = []
        for _, row in df.iterrows():
            blocks.append({
                'block_code': str(row.get('板块代码', '')),
                'block_name': str(row.get('板块名称', '')),
                'block_type': block_type,
                'stock_count': int(row.get('公司数量', 0)),
                'avg_change_pct': float(row.get('涨跌幅', 0)),
                'total_amount': float(row.get('总成交额', 0)) if '总成交额' in row else None,
                'leader_code': str(row.get('领涨股票代码', '')) if '领涨股票代码' in row else None,
                'leader_name': str(row.get('领涨股票', '')) if '领涨股票' in row else None,
            })
        return blocks
    except Exception as e:
        safe_stderr_print(f"[MarketBlocks] AKShare失败: {e}")
        return []


async def get_market_blocks(
    block_type: str = 'industry',
    limit: Optional[int] = None
) -> Dict[str, Any]:
    """
    获取市场板块数据
    数据源优先级: DB缓存(30min) → 东方财富直接HTTP → AKShare
    """
    if block_type not in ('industry', 'concept', 'region'):
        response = fail(f'Invalid block_type: {block_type}. Use: industry, concept, region')
        response["source"] = "none"
        return _with_provider_contract(
            response,
            "get_market_blocks",
            standard_model="MarketBlocks",
            provider_used="none",
            fallback_reason=f"invalid block_type: {block_type}",
        )
    if limit is not None and int(limit) <= 0:
        response = fail("limit 必须为正整数")
        response["source"] = "none"
        return _with_provider_contract(
            response,
            "get_market_blocks",
            standard_model="MarketBlocks",
            provider_used="none",
            fallback_reason="invalid limit",
        )

    source = 'none'
    blocks = []
    attempted_sources: list[str] = []

    # 1. DB缓存
    attempted_sources.append('db')
    blocks = await _fetch_from_db(block_type, limit)
    if blocks:
        source = 'db'

    # 2. sector_spot (新浪接口, 不受Clash代理影响, 仅行业)
    if not blocks and block_type == 'industry':
        attempted_sources.append('sina_sector')
        blocks = _fetch_from_sector_spot()
        if blocks:
            source = 'sina_sector'

    # 3. 同花顺板块 (AKShare THS接口)
    if not blocks:
        attempted_sources.append('ths')
        blocks = _fetch_from_ths(block_type)
        if blocks:
            source = 'ths'

    # 4. AKShare 东财接口(最后降级, push2可能被代理拦截)
    if not blocks:
        attempted_sources.append('akshare')
        blocks = _fetch_from_akshare(block_type)
        if blocks:
            source = 'akshare'

    # 5. 地域板块降级：地区成交汇总
    if not blocks and block_type == 'region':
        attempted_sources.append('akshare_area_summary')
        blocks = _fetch_region_area_summary()
        if blocks:
            source = 'akshare_area_summary'

    if not blocks:
        attempted_sources.append('db_stale')
        blocks = await _fetch_from_db_any_age(block_type, limit)
        if blocks:
            source = 'db_stale'

    if not blocks:
        tried = ",".join(attempted_sources) or "none"
        response = ok({
            'blocks': [],
            'count': 0,
            'block_type': block_type,
            'source': 'none',
            'degraded': True,
            'fallback_reason': f"all sources failed: {tried}",
        })
        response["source"] = "none"
        response["degraded"] = True
        return _with_provider_contract(
            response,
            "get_market_blocks",
            standard_model="MarketBlocks",
            provider_used="none",
            source_chain=attempted_sources,
            fallback_reason=f"all sources failed: {tried}",
        )

    if limit:
        blocks = blocks[:limit]

    if source in {'ths', 'db_stale'}:
        blocks = await _enrich_blocks_from_cache(blocks)

    blocks = _sanitize_placeholder_blocks(blocks, block_type)

    # 非DB来源时写入DB缓存
    if source not in {'db', 'db_stale'}:
        try:
            db = get_db()
            await _save_blocks_to_db(db, blocks)
        except Exception as e:
            safe_stderr_print(f"[MarketBlocks] 写DB失败: {e}")

    safe_stderr_print(f"[MarketBlocks] 成功获取 {len(blocks)} 个{block_type}板块 (source={source})")

    result = ok(
        {
            'blocks': normalize_block_list(blocks),
            'count': len(blocks),
            'block_type': block_type,
            'source': source,
            # P3-5.10 fix: 明示 source_priority 顺序(诊断报告 §5.10)
            # 历史问题:多 manager 走 get_market_blocks,但谁先 db 谁先 api 不一致;AI 看不到优先级
            'source_priority': ['db', 'sina_sector', 'ths', 'akshare', 'akshare_area_summary', 'db_stale'],
            'sources_attempted': attempted_sources,
        },
        cached=source in {"db", "db_stale"},
    )
    result["source"] = source
    degraded = any(bool(item.get('degraded')) for item in blocks)
    fallback_reason = next((str(item.get('fallback_reason')) for item in blocks if item.get('fallback_reason')), None)
    if degraded or fallback_reason:
        result['data']['degraded'] = True
    if fallback_reason:
        result['data']['fallback_reason'] = fallback_reason
    return _with_provider_contract(
        result,
        "get_market_blocks",
        standard_model="MarketBlocks",
        provider_used=source,
        source_chain=attempted_sources,
        fallback_reason=fallback_reason,
    )


async def get_block_stocks(block_code: str, limit: int | None = None) -> Dict[str, Any]:
    """
    获取板块成分股

    Args:
        block_code: 板块代码（支持 new_xxx 格式的新浪label 和 88xxxx 格式的同花顺代码）

    Returns:
        成分股列表
    """
    # 0. tqcenter 主路径：880xxx / 881xxx 格式直接走 SDK
    if (block_code.endswith(".SH") or block_code.endswith(".SZ")) or block_code.startswith(("88", "881")):
        try:
            from ..data_source import data_source as _ds
            members = await asyncio.to_thread(
                _ds.get_stock_list_in_sector, block_code, 0, 1
            )
            if members:
                stocks = []
                for item in members:
                    if isinstance(item, dict):
                        full = str(item.get("Code", ""))
                        bare = full.split(".")[0] if "." in full else full
                        stocks.append({
                            "code": bare,
                            "full_code": full,
                            "name": str(item.get("Name", "")),
                        })
                    elif isinstance(item, str):
                        bare = item.split(".")[0] if "." in item else item
                        stocks.append({"code": bare, "full_code": item, "name": ""})
                if stocks:
                    applied_limit: int | None = None
                    if limit is not None:
                        try:
                            applied_limit = max(int(limit), 0)
                        except (TypeError, ValueError):
                            applied_limit = None
                    display_stocks = stocks[:applied_limit] if applied_limit is not None else stocks
                    return _with_provider_contract(
                        ok({
                            "block_code": block_code,
                            "stocks": display_stocks,
                            "count": len(display_stocks),
                            "total_count": len(stocks),
                            "limit": applied_limit,
                            "truncated": applied_limit is not None and len(stocks) > applied_limit,
                            "source": "tqcenter.get_stock_list_in_sector",
                        }),
                        "get_block_stocks",
                        standard_model="BlockStocks",
                        provider_used="tqcenter.get_stock_list_in_sector",
                        source_chain=["tqcenter.get_stock_list_in_sector"],
                    )
        except Exception as exc:
            safe_stderr_print(f"[BlockStocks] tqcenter sector lookup failed: {exc}")

    if block_code.startswith("region::"):
        response = fail(f'Block {block_code} 来自地区成交汇总降级源，当前不提供成分股列表')
        response["source"] = "none"
        return _with_provider_contract(
            response,
            "get_block_stocks",
            standard_model="BlockStocks",
            provider_used="none",
            fallback_reason="region block has no constituents",
        )

    async def _resolve_block_meta(code: str) -> tuple[str | None, str | None]:
        guessed_type = None
        if code.startswith('new_'):
            guessed_type = 'industry'
        elif code.startswith('region::'):
            guessed_type = 'region'
        elif code.upper().startswith('BK'):
            guessed_type = 'concept'
        elif code.isdigit() and code.startswith('30'):
            guessed_type = 'concept'

        try:
            db = get_db()
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT block_name, block_type FROM market_blocks WHERE block_code = $1 ORDER BY updated_at DESC LIMIT 1",
                    code,
                )
                if row:
                    row = dict(row)
                    return str(row.get('block_name') or ''), str(row.get('block_type') or guessed_type or '')
        except Exception as e:
            safe_stderr_print(f"[BlockStocks] 读取 block meta 失败: {e}")

        search_types = [guessed_type] if guessed_type else ['industry', 'concept']
        for block_type in [bt for bt in search_types if bt]:
            try:
                blocks_res = await get_market_blocks(block_type=block_type, limit=500)
                if not blocks_res.get('success'):
                    continue
                for item in blocks_res.get('data', {}).get('blocks', []):
                    item_code = item.get('code') or item.get('blockCode') or item.get('block_code')
                    if str(item_code or '') == code:
                        item_name = item.get('name') or item.get('blockName') or item.get('block_name')
                        return str(item_name or ''), block_type
            except Exception as e:
                safe_stderr_print(f"[BlockStocks] 拉取 {block_type} block meta 失败: {e}")
        return None, guessed_type

    async def _load_cached_block_stocks(code: str, resolved_name: str | None) -> list[dict[str, Any]]:
        try:
            db = get_db()
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT stock_code, stock_name
                       FROM block_stocks
                       WHERE block_code = $1
                       ORDER BY stock_code ASC""",
                    code,
                )
                if not rows and resolved_name:
                    rows = await conn.fetch(
                        """SELECT DISTINCT bs.stock_code, bs.stock_name
                           FROM block_stocks bs
                           JOIN market_blocks mb
                             ON mb.block_code = bs.block_code
                          WHERE mb.block_name = $1
                          ORDER BY bs.stock_code ASC
                          LIMIT 500""",
                        resolved_name,
                    )
            cached_items = []
            for row in rows:
                item = dict(row)
                if not str(item.get('stock_code') or '').strip():
                    continue
                cached_items.append({
                    'stock_code': str(item.get('stock_code') or ''),
                    'stock_name': str(item.get('stock_name') or ''),
                    'change_pct': 0.0,
                    'price': 0.0,
                    'volume': 0,
                    'amount': 0.0,
                    '_source': 'db_cache',
                })
            return cached_items
        except Exception as e:
            safe_stderr_print(f"[BlockStocks] 读取 block_stocks 缓存失败: {e}")
            return []

    def _append_from_df(df, source_tag: str):
        if df is None or df.empty:
            return
        for _, row in df.iterrows():
            stock_code = str(row.get('代码', row.get('code', '')) or '')
            if not stock_code:
                continue
            stocks.append({
                'stock_code': stock_code,
                'stock_name': str(row.get('名称', row.get('name', '')) or ''),
                'change_pct': float(row.get('涨跌幅', row.get('changepercent', 0)) or 0),
                'price': float(row.get('最新价', row.get('trade', 0)) or 0),
                'volume': int(float(row.get('成交量', row.get('volume', 0)) or 0)),
                'amount': float(row.get('成交额', row.get('amount', 0)) or 0),
                '_source': source_tag,
            })

    stocks = []
    attempted: list[str] = []
    block_name, block_type = await _resolve_block_meta(block_code)
    candidate_symbols = []
    for candidate in (block_code, block_name):
        text = str(candidate or '').strip()
        if text and text not in candidate_symbols:
            candidate_symbols.append(text)

    if ak is None:
        attempted.append('db_cache')
        stocks = await _load_cached_block_stocks(block_code, block_name)
        if not stocks:
            result = ok({
                'block_code': block_code,
                'block_name': block_name,
                'block_type': block_type,
                'stocks': [],
                'count': 0,
                'source': 'none',
                'degraded': True,
                'fallback_reason': 'akshare not available and db cache empty',
            })
            result["source"] = "none"
            result["degraded"] = True
            return _with_provider_contract(
                result,
                "get_block_stocks",
                standard_model="BlockStocks",
                provider_used="none",
                source_chain=attempted,
                fallback_reason="akshare not available and db cache empty",
            )

    # 路径0: BK 前缀（东财板块代码 → 从板块列表反查名称 → 拉成分股）
    if block_code.upper().startswith('BK') and not stocks:
        resolved_bk_name = block_name
        if not resolved_bk_name:
            for board_type, list_fn in [
                ('concept', lambda: ak.stock_board_concept_name_em()),
                ('industry', lambda: ak.stock_board_industry_name_em()),
            ]:
                try:
                    df_boards = list_fn()
                    if df_boards is not None and not df_boards.empty:
                        code_col = '板块代码' if '板块代码' in df_boards.columns else None
                        name_col = '板块名称' if '板块名称' in df_boards.columns else None
                        if code_col and name_col:
                            match = df_boards[df_boards[code_col].astype(str) == block_code.upper()]
                            if not match.empty:
                                resolved_bk_name = str(match.iloc[0][name_col])
                                block_name = resolved_bk_name
                                block_type = board_type
                                break
                except Exception as e:
                    safe_stderr_print(f"[BlockStocks] BK板块列表查询失败({board_type}): {e}")
        if resolved_bk_name:
            for api_fn, tag in [
                (lambda s: ak.stock_board_concept_cons_em(symbol=s), 'eastmoney_concept_cons'),
                (lambda s: ak.stock_board_industry_cons_em(symbol=s), 'eastmoney_industry_cons'),
            ]:
                try:
                    attempted.append(f'{tag}:{resolved_bk_name}')
                    df = api_fn(resolved_bk_name)
                    _append_from_df(df, tag)
                    if stocks:
                        break
                except Exception as e:
                    safe_stderr_print(f"[BlockStocks] {tag}失败({resolved_bk_name}): {e}")

    # 路径1: 新浪 sector_detail（适用于 new_xxx 格式 label）
    if not stocks and block_code.startswith('new_'):
        try:
            attempted.append(f'sina_sector_detail:{block_code}')
            df = ak.stock_sector_detail(sector=block_code)
            _append_from_df(df, 'sina_sector_detail')
        except Exception as e:
            safe_stderr_print(f"[BlockStocks] sector_detail失败: {e}")

    if not stocks and block_type in (None, '', 'industry'):
        for symbol in candidate_symbols:
            try:
                attempted.append(f'eastmoney_industry_cons:{symbol}')
                df = ak.stock_board_industry_cons_em(symbol=symbol)
                _append_from_df(df, 'eastmoney_industry_cons')
                if stocks:
                    break
            except Exception as e:
                safe_stderr_print(f"[BlockStocks] stock_board_industry_cons_em失败({symbol}): {e}")

    if not stocks and block_type in (None, '', 'concept'):
        for symbol in candidate_symbols:
            try:
                attempted.append(f'eastmoney_concept_cons:{symbol}')
                df = ak.stock_board_concept_cons_em(symbol=symbol)
                _append_from_df(df, 'eastmoney_concept_cons')
                if stocks:
                    break
            except Exception as e:
                safe_stderr_print(f"[BlockStocks] stock_board_concept_cons_em失败({symbol}): {e}")

    if not stocks and block_type in (None, '', 'concept'):
        attempted.append('ths_concept_detail')
        stocks = _fetch_concept_stocks_from_ths(block_code, block_name)

    # Tushare concept + concept_detail 降级（按板块名匹配）
    if not stocks:
        search_name = block_name or (block_code if not block_code.upper().startswith('BK') and not block_code.startswith('new_') else None)
        if search_name:
            try:
                from ..data_source import data_source
                ts_pro = data_source.get_tushare_pro()
                if ts_pro:
                    attempted.append(f'tushare_concept:{search_name}')
                    df_concepts = ts_pro.concept()
                    if df_concepts is not None and not df_concepts.empty:
                        # 精确匹配优先，再模糊匹配
                        exact = df_concepts[df_concepts['name'] == search_name]
                        fuzzy = df_concepts[df_concepts['name'].str.contains(search_name[:2], na=False)] if not exact.empty is False else exact
                        match = exact if not exact.empty else (fuzzy if not fuzzy.empty else None)
                        if match is not None and not match.empty:
                            concept_id = str(match.iloc[0]['code'])
                            concept_nm = str(match.iloc[0]['name'])
                            if not block_name:
                                block_name = concept_nm
                            df_detail = ts_pro.concept_detail(id=concept_id, fields="ts_code,name,in_date")
                            if df_detail is not None and not df_detail.empty:
                                for _, row in df_detail.iterrows():
                                    ts_c = str(row.get('ts_code', '') or '')
                                    raw_code = ts_c.split('.')[0] if '.' in ts_c else ts_c
                                    if raw_code:
                                        stocks.append({
                                            'stock_code': raw_code,
                                            'stock_name': str(row.get('name', '') or ''),
                                            'change_pct': 0.0,
                                            'price': 0.0,
                                            'volume': 0,
                                            'amount': 0.0,
                                            '_source': 'tushare_concept_detail',
                                        })
            except Exception as e:
                safe_stderr_print(f"[BlockStocks] Tushare concept查询失败: {e}")

    if not stocks:
        attempted.append('db_cache')
        stocks = await _load_cached_block_stocks(block_code, block_name)

    if not stocks:
        resolved = f", resolved_name={block_name}" if block_name else ""
        tried = ", tried=" + "|".join(attempted) if attempted else ""
        eastmoney_down = any('eastmoney' in a for a in attempted) and not any('sina' in a for a in attempted and stocks)
        hint = ""
        if block_code.upper().startswith('BK') or (not block_code.startswith('new_') and block_type in ('concept', None)):
            hint = "。提示: 东财概念板块 API 可能因代理拦截不可用，请尝试使用 new_xxx 格式的新浪行业代码（如 new_dlhy=电力行业），或直接传入中文板块名称"
        response = fail(f'板块 {block_code} 未找到成分股{resolved}{tried}{hint}')
        response["source"] = "none"
        response["data"] = {
                'block_code': block_code,
                'block_name': block_name,
                'block_type': block_type,
                'tried': attempted,
                'fallback_reason': 'upstream_api_unavailable' if eastmoney_down else 'upstream_unavailable_or_code_system_mismatch',
            }
        return _with_provider_contract(
            response,
            "get_block_stocks",
            standard_model="BlockStocks",
            provider_used="none",
            source_chain=attempted,
            fallback_reason=response["data"]["fallback_reason"],
        )

    source = stocks[0].get('_source') if stocks else None
    for item in stocks:
        item.pop('_source', None)

    try:
        db = get_db()
        async with db.acquire() as conn:
            for item in stocks:
                await conn.execute(
                    """INSERT INTO block_stocks (block_code, stock_code, stock_name, updated_at)
                       VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                       ON CONFLICT (block_code, stock_code) DO UPDATE SET
                           stock_name = EXCLUDED.stock_name,
                           updated_at = CURRENT_TIMESTAMP""",
                    block_code,
                    item.get('stock_code'),
                    item.get('stock_name'),
                )
            await conn.execute(
                """UPDATE market_blocks
                   SET stock_count=$1, updated_at=CURRENT_TIMESTAMP
                   WHERE block_code=$2""",
                len(stocks),
                block_code,
            )
    except Exception as e:
        safe_stderr_print(f"[BlockStocks] 写 block_stocks 缓存失败: {e}")

    normalized_stocks = normalize_block_stock_list(stocks)
    total_count = len(normalized_stocks)
    applied_limit: int | None = None
    if limit is not None:
        try:
            applied_limit = max(int(limit), 0)
        except (TypeError, ValueError):
            applied_limit = None
    display_stocks = normalized_stocks
    if applied_limit is not None:
        display_stocks = normalized_stocks[:applied_limit]

    result = ok(
        {
            'block_code': block_code,
            'block_name': block_name,
            'block_type': block_type,
            'stocks': display_stocks,
            'count': len(display_stocks),
            'total_count': total_count,
            'limit': applied_limit,
            'truncated': applied_limit is not None and total_count > applied_limit,
            'source': source,
        }
    )
    result["source"] = str(source or "market_blocks")
    # P2-4.5.5 fix: db_cache 残缺标 warning(诊断报告 §4.5.5)
    # 历史问题:db_cache 路径只填 stock_code/stock_name,价格/涨跌幅/成交量全 0
    # AI 不知 0 是真实值还是 cache 残缺,导致下游计算误判
    if str(source or "") == "db_cache":
        zero_price_count = sum(
            1 for item in display_stocks
            if (item.get('price') in (0, 0.0)) and (item.get('change_pct') in (0, 0.0))
        )
        warnings = []
        if zero_price_count > 0:
            warnings.append({
                'code': 'db_cache_partial_quote',
                'message': f'db_cache 来源: {zero_price_count}/{len(display_stocks)} 条无实时价格/涨跌数据,price/change_pct/volume/amount 字段不可用,仅 stock_code/stock_name 可信',
                'severity': 'warning',
                'fields_unreliable': ['price', 'change_pct', 'volume', 'amount'],
            })
        if warnings:
            result['warnings'] = warnings
            result['quality_flags'] = ['db_cache_quote_missing']
            result['cache_only'] = True
    return _with_provider_contract(
        result,
        "get_block_stocks",
        standard_model="BlockStocks",
        provider_used=str(source or "market_blocks"),
        source_chain=attempted or [str(source or "market_blocks")],
    )


async def _save_blocks_to_db(db, blocks: List[Dict[str, Any]]) -> None:
    """保存板块数据到数据库"""
    try:
        async with db.acquire() as conn:
            for block in blocks:
                await conn.execute("""
                    INSERT INTO market_blocks (
                        block_code, block_name, block_type, stock_count,
                        avg_change_pct, total_amount, leader_code, leader_name, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, CURRENT_TIMESTAMP)
                    ON CONFLICT (block_code, block_type) DO UPDATE SET
                        block_name = EXCLUDED.block_name,
                        stock_count = EXCLUDED.stock_count,
                        avg_change_pct = EXCLUDED.avg_change_pct,
                        total_amount = EXCLUDED.total_amount,
                        leader_code = EXCLUDED.leader_code,
                        leader_name = EXCLUDED.leader_name,
                        updated_at = CURRENT_TIMESTAMP
                """,
                    block['block_code'],
                    block['block_name'],
                    block['block_type'],
                    block['stock_count'],
                    block['avg_change_pct'],
                    block['total_amount'],
                    block['leader_code'],
                    block['leader_name']
                )
    except Exception as e:
        safe_stderr_print(f"[MarketBlocks] Failed to save to DB: {e}")
