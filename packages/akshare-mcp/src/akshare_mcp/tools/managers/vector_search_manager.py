"""向量搜索管理器 - 相似K线形态、相似股票"""

from typing import Any, Optional
import time

from ...storage import get_db
from ...utils import normalize_code, propagate_data_quality_to_top
from ...services.retrieval_eval import summarize_ranked_results
from ..manager_protocol import (
    normalize_manager_payload,
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    ok_with_meta,
)


def register_vector_search_manager(mcp):
    """注册向量搜索管理器工具"""

    async def _run_registered_tool(tool_name: str, args: dict) -> dict:
        tool = getattr(getattr(mcp, "_tool_manager", None), "_tools", {}).get(tool_name)
        if tool is None:
            return {"success": False, "error": f"Tool {tool_name} not found"}
        result = await tool.run(args or {})
        return result if isinstance(result, dict) else {"success": False, "error": f"Unexpected result type from {tool_name}"}
    
    @mcp.tool()
    async def vector_search_manager(action: str, params: dict | None = None, kwargs: Any = None, code: str | None = None, query: str | None = None, top_n: int | None = None, days: int | None = None, similarity_type: str | None = None, doc_types: list[str] | None = None, limit: int | None = None, search_backend: str | None = None):
        """向量搜索管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/similar_patterns/similar_stocks/market_docs
            code (str, optional): 股票代码
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - similar_patterns: code(str, 目标股票), days(int, optional, K线天数), top_n(int, optional)
                - similar_stocks: code(str, 目标股票), similarity_type(str, optional, "fundamental"/"technical"/"both"), top_n(int, optional)
                - market_docs: code(str, 股票代码), query(str, optional), doc_types(list[str], optional), limit(int, optional)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            vector_search_manager(action="help", kwargs="{}")
            # 搜索相似K线形态
            vector_search_manager(action="similar_patterns", code="600519", kwargs='{"days":20,"top_n":5}')
            # 搜索相似股票
            vector_search_manager(action="similar_stocks", code="600519", kwargs='{"similarity_type":"both","top_n":10}')
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "code": code,
                    "query": query,
                    "top_n": top_n,
                    "days": days,
                    "similarity_type": similarity_type,
                    "doc_types": doc_types,
                    "limit": limit,
                    "search_backend": search_backend,
                },
            )
            kwargs = normalize_manager_kwargs(kwargs)
            code, kwargs = normalize_manager_code(code, kwargs)

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name='vector_search_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name='vector_search_manager',
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'similar_patterns': '搜索相似K线形态（需要 code）',
                        'similar_stocks': '搜索相似股票（需要 code）',
                        'market_docs': '搜索市场文本 chunk（支持 hybrid retrieval）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['vector_search_manager'])
            
            elif action == 'similar_patterns':
                if not code:
                    return _fail('需要提供股票代码')
                
                code = normalize_code(code)
                top_n = kwargs.get('top_n', 10)
                days = kwargs.get('days', 20)
                
                search_backend = str(kwargs.get('search_backend', 'db') or 'db')
                allow_fallback = bool(kwargs.get('allow_fallback', True))

                # 调用真实的 K线形态搜索
                try:
                    result = await _run_registered_tool(
                        "search_by_kline",
                        {
                            "code": code,
                            "days": int(days),
                            "top_n": int(top_n),
                            "search_backend": search_backend,
                            "allow_fallback": allow_fallback,
                        },
                    )
                    if not result.get("success"):
                        return _fail(
                            result.get("error") or f"search_by_kline failed for {code}",
                            source_chain=['vector_search_manager', 'search_by_kline'],
                        )
                    if result.get('success') and result.get('data'):
                        data = result['data']
                        similar = data.get('results', [])
                        backend_requested = data.get('backend_requested') or data.get('search_backend') or 'python'
                        backend_used = data.get('backend_used') or data.get('actual_backend') or backend_requested
                        retrieval_quality = summarize_ranked_results(
                            similar,
                            score_key='similarity',
                            backend_requested=backend_requested,
                            backend_used=backend_used,
                            fallback_used=bool(data.get('fallback_used', backend_used != backend_requested)),
                            fallback_reason=data.get('fallback_reason'),
                        )
                        return propagate_data_quality_to_top(_ok({
                            'code': code,
                            'similar_stocks': similar,
                            'pattern_type': 'kline_similarity',
                            'confidence': similar[0].get('similarity', 0) if similar else 0,
                            'source': data.get('actual_backend') or data.get('search_backend') or 'kline_correlation',
                            'backend_requested': backend_requested,
                            'backend_used': backend_used,
                            'fallback_used': bool(data.get('fallback_used', backend_used != backend_requested)),
                            'fallback_reason': data.get('fallback_reason'),
                            'degraded': bool(data.get('degraded') or result.get('degraded') or data.get('fallback_used')),
                            'source_chain': data.get('source_chain') or result.get('source_chain') or ['vector_search_manager', 'search_by_kline'],
                            'quality_flags': data.get('quality_flags') or result.get('quality_flags') or ([] if similar else ['empty_result']),
                            'latency_ms': data.get('latency_ms', 0),
                            'retrieval_quality': retrieval_quality,
                        }, source_chain=['vector_search_manager', 'search_by_kline']))
                except Exception as exc:
                    return _fail(
                        f"search_by_kline exception: {type(exc).__name__}: {exc}",
                        source_chain=['vector_search_manager', 'search_by_kline'],
                    )
                
                return _fail(
                    f"未检索到 {code} 的相似K线形态数据，请先运行数据预热",
                    source_chain=['vector_search_manager', 'search_by_kline'],
                )
            
            elif action == 'similar_stocks':
                if not code:
                    return _fail('需要提供股票代码')
                
                code = normalize_code(code)
                top_n = kwargs.get('top_n', 10)
                similarity_type = kwargs.get('similarity_type', 'both')
                search_backend = str(kwargs.get('search_backend', 'db') or 'db')
                allow_fallback = bool(kwargs.get('allow_fallback', True))
                
                # 调用真实的相似股票搜索
                try:
                    result = await _run_registered_tool(
                        "search_similar_stocks",
                        {
                            "code": code,
                            "top_n": int(top_n),
                            "similarity_type": str(similarity_type),
                            "search_backend": search_backend,
                            "allow_fallback": allow_fallback,
                        },
                    )
                    if not result.get("success"):
                        return _fail(
                            result.get("error") or f"search_similar_stocks failed for {code}",
                            source_chain=['vector_search_manager', 'search_similar_stocks'],
                        )
                    if result.get('success') and result.get('data'):
                        data = result['data']
                        similar = data.get('similar_stocks', [])
                        backend_requested = data.get('backend_requested') or data.get('search_backend') or 'python'
                        backend_used = data.get('backend_used') or data.get('actual_backend') or backend_requested
                        retrieval_quality = summarize_ranked_results(
                            similar,
                            score_key='similarity',
                            backend_requested=backend_requested,
                            backend_used=backend_used,
                            fallback_used=bool(data.get('fallback_used', backend_used != backend_requested)),
                            fallback_reason=data.get('fallback_reason'),
                        )
                        return propagate_data_quality_to_top(_ok({
                            'code': code,
                            'similar_stocks': similar,
                            'source': data.get('candidate_scope', 'similarity_search'),
                            'similarity_type': data.get('similarity_type', similarity_type),
                            'backend_requested': backend_requested,
                            'backend_used': backend_used,
                            'fallback_used': bool(data.get('fallback_used', backend_used != backend_requested)),
                            'fallback_reason': data.get('fallback_reason'),
                            'degraded': bool(data.get('degraded') or result.get('degraded') or data.get('fallback_used')),
                            'source_chain': data.get('source_chain') or result.get('source_chain') or ['vector_search_manager', 'search_similar_stocks'],
                            'quality_flags': data.get('quality_flags') or result.get('quality_flags') or ([] if similar else ['empty_result']),
                            'latency_ms': data.get('latency_ms', 0),
                            'retrieval_quality': retrieval_quality,
                        }, source_chain=['vector_search_manager', 'search_similar_stocks']))
                except Exception as exc:
                    return _fail(
                        f"search_similar_stocks exception: {type(exc).__name__}: {exc}",
                        source_chain=['vector_search_manager', 'search_similar_stocks'],
                    )
                
                return _fail(
                    f"未检索到 {code} 的相似股票数据，请先运行数据预热",
                    source_chain=['vector_search_manager', 'search_similar_stocks'],
                )

            elif action == 'market_docs':
                if code:
                    code = normalize_code(code)
                query_text = str(kwargs.get('query') or kwargs.get('query_text') or '').strip()
                raw_doc_types = kwargs.get('doc_types') or kwargs.get('doc_type') or []
                if isinstance(raw_doc_types, str):
                    doc_types = [item.strip().lower() for item in raw_doc_types.replace(';', ',').split(',') if item.strip()]
                else:
                    doc_types = [str(item).strip().lower() for item in list(raw_doc_types or []) if str(item).strip()]
                limit = int(kwargs.get('limit', 10) or 10)
                if not code and not query_text:
                    return _fail('需要提供股票代码或查询词')

                try:
                    rows = await db.search_market_doc_chunks(
                        query_text=query_text or None,
                        stock_code=code or None,
                        doc_types=doc_types or None,
                        start_date=kwargs.get('start_date'),
                        end_date=kwargs.get('end_date'),
                        limit=limit,
                    ) if hasattr(db, 'search_market_doc_chunks') else []
                except Exception as exc:
                    return _fail(
                        f"search_market_doc_chunks exception: {type(exc).__name__}: {exc}",
                        source_chain=['vector_search_manager', 'search_market_doc_chunks'],
                    )

                retrieval_mode = 'recent'
                if query_text:
                    retrieval_mode = 'hybrid'
                if query_text and not rows:
                    retrieval_mode = 'hybrid_empty'

                return _ok({
                    'code': code,
                    'query': query_text,
                    'doc_types': doc_types,
                    'count': len(rows),
                    'retrieval_mode': retrieval_mode,
                    'results': rows,
                }, source_chain=['vector_search_manager', 'search_market_doc_chunks'])
            
            else:
                return _fail(f'Unknown action: {action}. Supported: help, similar_patterns, similar_stocks, market_docs')
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name='vector_search_manager',
                action=action,
                started_at=start_time,
                source_chain=['vector_search_manager'],
            )
