from ._vector_common import *
from ..utils import propagate_data_quality_to_top


def _is_excluded_stock_name(name: str) -> bool:
    """P0-4 fix: filter out ST/退市/暂停上市 stocks from kline-similarity results.

    These stocks can have low-volatility flat-line K shapes that score high in cosine similarity
    but are useless as investment references (see S10-F04 茅台 5/5 全 ST 退市股 case).
    Filter rules cover common A-share abnormal markers from stock_name field.
    """
    if not name:
        return False
    txt = str(name).strip().upper()
    # 常见异常标记: *ST / ST / 退 / 退市 / 暂停 / *退
    if txt.startswith('*ST') or txt.startswith('ST'):
        return True
    if '退' in txt or '暂停' in txt or 'PT' in txt:
        return True
    return False


async def search_by_kline(
    code: str,
    days: int = 20,
    top_n: int = 10,
    search_backend: str = 'db',
    allow_fallback: bool = True,
):
    """
    基于K线形态搜索相似股票 - 使用向量搜索引擎。

    Args:
        code: 股票代码
        days: K线天数
        top_n: 返回数量
        search_backend: 检索后端（db/python/index）
        allow_fallback: 检索失败时是否回退 python
    """
    try:
        db = get_db()
        backend_requested = str(search_backend or 'db').strip().lower()
        if backend_requested in {'sqlite_python', 'sqlite'}:
            backend_requested = 'db'
        if backend_requested not in {'db', 'python', 'index'}:
            backend_requested = 'python'
        fallback_reason = None

        # 1. 获取目标股票K线
        target_lookback = max(int(days) * 4, 60)
        target_klines = await db.get_klines(code, limit=target_lookback)
        if not target_klines or len(target_klines) < days:
            return fail(f'Insufficient kline data for {code}')
        target_klines = list(target_klines)[-int(days):]

        # 2. 获取目标股票信息
        target_info = await db.get_stock_info(code)
        target_industry = target_info.get('industry', '') if target_info else ''

        # 3. 查找候选股票
        _, candidate_rows = await _load_candidate_stock_rows(db, code, target_industry, limit=100)
        # P0-4 fix: 入口过滤 ST/退市/暂停上市股票,避免低波动平直 K 线推高 cosine 相似度
        excluded_count_before = len(candidate_rows)
        candidate_rows = [
            row for row in candidate_rows
            if not _is_excluded_stock_name(str(row.get('stock_name', '') or ''))
        ]
        excluded_st_count = excluded_count_before - len(candidate_rows)
        candidate_rows = candidate_rows[:50]
        candidates = {row['code']: row.get('stock_name', '') for row in candidate_rows if row.get('code')}
        if not candidates:
            return fail('No candidate stocks found (after ST/delisted filter)')

        search_meta = {
            'backend_requested': backend_requested,
            'backend_used': backend_requested,
            'fallback_used': False,
            'fallback_reason': None,
            'latency_ms': 0.0,
        }
        results = []
        candidate_klines_loaded = 0

        if backend_requested == 'db':
            try:
                query_vector = vector_search_engine.kline_to_vector(target_klines, method='returns')
                query_vector_values = _normalize_kline_vector(query_vector.tolist() if hasattr(query_vector, 'tolist') else query_vector)
                if query_vector_values and hasattr(db, 'search_kline_pattern_windows'):
                    db_rows = await db.search_kline_pattern_windows(
                        query_embedding=query_vector_values,
                        window_size=int(days),
                        vector_method='returns',
                        period='daily',
                        adjust='',
                        stock_codes=list(candidates.keys()),
                        exclude_stock_code=code,
                        limit=max(int(top_n) * 5, 20),
                    )
                    if db_rows:
                        results = [
                            {
                                'code': str(item.get('stock_code') or ''),
                                'name': item.get('stock_name') or candidates.get(str(item.get('stock_code') or ''), ''),
                                'similarity': round(float(item.get('similarity') or 0.0), 4),
                                'source': 'db',
                                'end_date': _coerce_date_str(item.get('end_date')),
                                'start_date': _coerce_date_str(item.get('start_date')),
                                'forward_return_5d': item.get('forward_return_5d'),
                                'forward_return_10d': item.get('forward_return_10d'),
                                'forward_return_20d': item.get('forward_return_20d'),
                            }
                            for item in db_rows[:int(top_n) * 3]  # 取 3x 候选,过滤后裁剪
                            if str(item.get('stock_code') or '')
                        ]
                        # P0-4 fix: 二次过滤,防御 db 返回的 stock_name 含 ST/退市标记
                        results = [r for r in results if not _is_excluded_stock_name(r.get('name', ''))][:int(top_n)]
                        candidate_klines_loaded = len(db_rows)
                        search_meta = {
                            'backend_requested': 'db',
                            'backend_used': 'db',
                            'fallback_used': False,
                            'fallback_reason': None,
                            'latency_ms': 0.0,
                        }
                    else:
                        fallback_reason = 'db_empty_result'
                else:
                    fallback_reason = 'db_backend_unsupported'
            except Exception as exc:
                fallback_reason = f'db_exception:{type(exc).__name__}'

            if results:
                return propagate_data_quality_to_top(ok({
                    'code': code,
                    'name': (target_info.get('name') or target_info.get('stock_name', '')) if target_info else '',
                    'days': days,
                    'results': results,
                    'total_candidates': len(candidates),
                    'candidate_klines_loaded': candidate_klines_loaded,
                    'calculated': len(results),
                    'search_backend': backend_requested,
                    'actual_backend': search_meta.get('backend_used', 'db'),
                    'allow_fallback': bool(allow_fallback),
                    'backend_requested': search_meta.get('backend_requested', 'db'),
                    'backend_used': search_meta.get('backend_used', 'db'),
                    'fallback_used': bool(search_meta.get('fallback_used', False)),
                    'fallback_reason': search_meta.get('fallback_reason'),
                    'latency_ms': search_meta.get('latency_ms', 0.0),
                    'excluded_st_count': excluded_st_count,
                    'quality_filter': 'st_delisted_excluded_at_input',
                }))

            if not allow_fallback:
                return propagate_data_quality_to_top(ok({
                    'code': code,
                    'name': (target_info.get('name') or target_info.get('stock_name', '')) if target_info else '',
                    'days': days,
                    'results': [],
                    'total_candidates': len(candidates),
                    'candidate_klines_loaded': 0,
                    'calculated': 0,
                    'search_backend': backend_requested,
                    'actual_backend': 'db',
                    'allow_fallback': bool(allow_fallback),
                    'backend_requested': 'db',
                    'backend_used': 'db',
                    'fallback_used': False,
                    'fallback_reason': fallback_reason or 'db_empty_result',
                    'latency_ms': 0.0,
                    'excluded_st_count': excluded_st_count,
                    'quality_filter': 'st_delisted_excluded_at_input',
                }))

        # 4. 获取候选K线并执行向量检索
        _, candidate_klines_dict = await _prefetch_candidate_context(
            db,
            candidate_rows,
            need_financials=False,
            need_klines=True,
            kline_limit=days,
        )
        candidate_klines_dict = {
            candidate_code: rows
            for candidate_code, rows in candidate_klines_dict.items()
            if rows and len(rows) >= days
        }

        if not candidate_klines_dict:
            return fail('No candidate kline data available')

        engine_backend = backend_requested if backend_requested in {'python', 'index'} else 'python'
        search_results = vector_search_engine.find_similar_patterns(
            query_klines=target_klines,
            candidate_klines_dict=candidate_klines_dict,
            top_k=top_n,
            method='returns',
            metric='correlation',
            backend=engine_backend,
            allow_fallback=allow_fallback,
        )
        search_meta = dict(getattr(vector_search_engine, 'last_meta', {}) or {})
        if backend_requested == 'db':
            search_meta['backend_requested'] = 'db'
            search_meta['fallback_used'] = True
            search_meta['fallback_reason'] = fallback_reason or search_meta.get('fallback_reason') or 'db_empty_result'

        results = []
        for item in search_results:
            candidate_code = item.get('code', '')
            similarity = float(item.get('similarity', 0.0))
            cand_name = candidates.get(candidate_code, '')
            # P0-4 fix: 二次过滤防御 fallback 路径
            if _is_excluded_stock_name(cand_name):
                continue
            results.append({
                'code': candidate_code,
                'name': cand_name,
                'similarity': round(similarity, 4),
                'source': item.get('source', vector_search_engine.last_backend_used),
            })
        candidate_klines_loaded = len(candidate_klines_dict)

        return propagate_data_quality_to_top(ok({
            'code': code,
            'name': (target_info.get('name') or target_info.get('stock_name', '')) if target_info else '',
            'days': days,
            'results': results,
            'total_candidates': len(candidates),
            'candidate_klines_loaded': candidate_klines_loaded,
            'calculated': len(results),
            'search_backend': backend_requested,
            'actual_backend': vector_search_engine.last_backend_used,
            'allow_fallback': bool(allow_fallback),
            'backend_requested': search_meta.get('backend_requested', backend_requested),
            'backend_used': search_meta.get('backend_used', vector_search_engine.last_backend_used),
            'fallback_used': bool(search_meta.get('fallback_used', False)),
            'fallback_reason': search_meta.get('fallback_reason'),
            'latency_ms': search_meta.get('latency_ms', 0.0),
            'excluded_st_count': excluded_st_count,
            'quality_filter': 'st_delisted_excluded_at_input',
        }))

    except Exception as e:
        return fail(str(e))
