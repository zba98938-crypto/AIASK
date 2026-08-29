from ._vector_common import *
from ..utils import propagate_data_quality_to_top

async def search_similar_stocks(
    code: str,
    top_n: int = 10,
    similarity_type: str = 'both',
    search_backend: str = 'db',
    allow_fallback: bool = True,
):
    """
    搜索相似股票 - 基于股票画像向量或基本面/技术面特征相似度

    Args:
        code: 股票代码
        top_n: 返回数量
        similarity_type: 相似度类型 ('fundamental'基本面, 'technical'技术面, 'both'综合)
        search_backend: 检索后端（db/python）
        allow_fallback: DB 检索失败时是否回退手工特征
    """
    try:
        db = get_db()
        resolved_similarity_type = str(similarity_type or 'both').strip().lower()
        if resolved_similarity_type not in {'fundamental', 'technical', 'both'}:
            resolved_similarity_type = 'both'
        backend_requested = str(search_backend or 'db').strip().lower()
        if backend_requested in {'sqlite_python', 'sqlite'}:
            backend_requested = 'db'
        if backend_requested not in {'db', 'python'}:
            backend_requested = 'python'

        # 1. 获取目标股票信息
        target_info = await db.get_stock_info(code)
        if not target_info:
            return propagate_data_quality_to_top(ok({
                'code': code,
                'name': '',
                'industry': '',
                'candidate_scope': 'unknown',
                'similar_stocks': [],
                'similarity_type': resolved_similarity_type,
                'total_candidates': 0,
                'calculated': 0,
                'search_backend': backend_requested,
                'actual_backend': 'none',
                'allow_fallback': bool(allow_fallback),
                'backend_requested': backend_requested,
                'backend_used': 'none',
                'fallback_used': bool(allow_fallback),
                'fallback_reason': 'target_stock_profile_missing',
                'latency_ms': 0.0,
                'degraded': True,
                'quality_flags': ['empty_upstream', 'target_missing'],
                'source_chain': ['db.stock_info', 'vector_search.similar_stocks'],
            }))

        target_industry = target_info.get('industry', '')
        candidate_scope, candidate_rows = await _load_candidate_stock_rows(db, code, target_industry, limit=100)
        candidate_rows = candidate_rows[:50]
        candidate_codes = [row['code'] for row in candidate_rows if row.get('code')]
        candidate_names = {row['code']: row.get('stock_name', '') for row in candidate_rows if row.get('code')}

        if not candidate_codes:
            return propagate_data_quality_to_top(ok({
                'code': code,
                'name': target_info.get('name') or target_info.get('stock_name', ''),
                'industry': target_industry,
                'candidate_scope': candidate_scope,
                'similar_stocks': [],
                'similarity_type': resolved_similarity_type,
                'total_candidates': 0,
                'calculated': 0,
                'search_backend': backend_requested,
                'actual_backend': 'none',
                'allow_fallback': bool(allow_fallback),
                'backend_requested': backend_requested,
                'backend_used': 'none',
                'fallback_used': bool(allow_fallback),
                'fallback_reason': 'candidate_stock_pool_empty',
                'latency_ms': 0.0,
                'degraded': True,
                'quality_flags': ['empty_upstream', 'candidate_pool_empty'],
                'source_chain': ['db.stock_universe', 'vector_search.similar_stocks'],
            }))

        fallback_reason = None
        if backend_requested == 'db':
            try:
                query_profile = await build_stock_profile_payload(
                    db,
                    code,
                    profile_type=resolved_similarity_type,
                    kline_limit=90,
                    version='v1',
                )
                used_db_search = False
                if query_profile and query_profile.get('embedding') and hasattr(db, 'search_vector_collection'):
                    used_db_search = True
                    dense_payload = await db.search_vector_collection(
                        query_embedding=list(query_profile.get('embedding') or []),
                        collection_name='stock_profile_embeddings',
                        profile_type=resolved_similarity_type,
                        stock_codes=candidate_codes,
                        exclude_stock_code=code,
                        limit=max(int(top_n) * 6, 20),
                        metric='cosine',
                    )
                    db_rows = list((dense_payload or {}).get('items') or [])
                elif query_profile and query_profile.get('embedding') and hasattr(db, 'search_vector_profiles_by_embedding'):
                    used_db_search = True
                    db_rows = await db.search_vector_profiles_by_embedding(
                        query_embedding=list(query_profile.get('embedding') or []),
                        collection_name='stock_profile_embeddings',
                        profile_type=resolved_similarity_type,
                        stock_codes=candidate_codes,
                        exclude_stock_code=code,
                        limit=max(int(top_n) * 6, 20),
                        metric='cosine',
                    )
                else:
                    db_rows = []
                if db_rows:
                    similar_stocks = []
                    for row in db_rows:
                        candidate_code = str(row.get('stock_code') or '').strip()
                        if not candidate_code:
                            continue
                        metadata = dict(row.get('metadata') or {})
                        similar_stocks.append({
                            'code': candidate_code,
                            'name': metadata.get('stock_name') or candidate_names.get(candidate_code, ''),
                            'similarity': round(float(row.get('similarity') or 0.0), 4),
                            'features': dict(metadata.get('raw_features') or {}),
                            'source': 'db',
                            'profile_type': row.get('profile_type') or resolved_similarity_type,
                        })
                    if similar_stocks:
                        similar_stocks.sort(key=lambda item: item['similarity'], reverse=True)
                        return propagate_data_quality_to_top(ok({
                            'code': code,
                            'name': target_info.get('name') or target_info.get('stock_name', ''),
                            'industry': target_industry,
                            'candidate_scope': candidate_scope,
                            'similar_stocks': similar_stocks[:top_n],
                            'similarity_type': resolved_similarity_type,
                            'total_candidates': len(candidate_rows),
                            'calculated': len(similar_stocks),
                            'search_backend': backend_requested,
                            'actual_backend': 'db',
                            'allow_fallback': bool(allow_fallback),
                            'backend_requested': 'db',
                            'backend_used': 'db',
                            'fallback_used': False,
                            'fallback_reason': None,
                            'latency_ms': 0.0,
                        }))
                fallback_reason = 'db_empty_result' if used_db_search else 'db_backend_unsupported'
            except Exception as exc:
                fallback_reason = f'db_exception:{type(exc).__name__}'

            if not allow_fallback:
                return propagate_data_quality_to_top(ok({
                    'code': code,
                    'name': target_info.get('name') or target_info.get('stock_name', ''),
                    'industry': target_industry,
                    'candidate_scope': candidate_scope,
                    'similar_stocks': [],
                    'similarity_type': resolved_similarity_type,
                    'total_candidates': len(candidate_rows),
                    'calculated': 0,
                    'search_backend': backend_requested,
                    'actual_backend': 'db',
                    'allow_fallback': bool(allow_fallback),
                    'backend_requested': 'db',
                    'backend_used': 'db',
                    'fallback_used': False,
                    'fallback_reason': fallback_reason or 'db_empty_result',
                    'latency_ms': 0.0,
                }))

        # 2. 获取目标股票特征
        target_financial_row = None
        target_klines = None
        if resolved_similarity_type in ['fundamental', 'both']:
            try:
                financials = await db.get_financials(code, limit=1)
                if financials:
                    target_financial_row = financials[0]
            except Exception:
                pass
        if resolved_similarity_type in ['technical', 'both']:
            try:
                target_klines = await db.get_klines(code, limit=60)
            except Exception:
                target_klines = None

        target_features = _build_target_features(
            dict(target_info or {}),
            target_financial_row,
            target_klines,
            resolved_similarity_type,
        )

        if not target_features:
            return propagate_data_quality_to_top(ok({
                'code': code,
                'name': target_info.get('name') or target_info.get('stock_name', ''),
                'industry': target_industry,
                'candidate_scope': candidate_scope,
                'similar_stocks': [],
                'similarity_type': resolved_similarity_type,
                'total_candidates': len(candidate_rows),
                'calculated': 0,
                'search_backend': backend_requested,
                'actual_backend': 'none',
                'allow_fallback': bool(allow_fallback),
                'backend_requested': backend_requested,
                'backend_used': 'none',
                'fallback_used': bool(allow_fallback),
                'fallback_reason': 'target_features_missing',
                'latency_ms': 0.0,
                'degraded': True,
                'quality_flags': ['empty_upstream', 'target_features_missing'],
                'source_chain': ['db.stock_info', 'db.financials', 'db.klines', 'vector_search.similar_stocks'],
            }))

        filled_stock_info = await _fill_missing_candidate_stock_info(db, candidate_rows)
        financial_map, kline_map = await _prefetch_candidate_context(
            db,
            candidate_rows,
            need_financials=resolved_similarity_type in ['fundamental', 'both'],
            need_klines=resolved_similarity_type in ['technical', 'both'],
            kline_limit=60,
        )

        # 4. 计算相似度
        candidate_payloads = []
        for candidate_row in candidate_rows:
            candidate_code = str(candidate_row.get('code') or '').strip()
            if not candidate_code:
                continue
            candidate_features = _build_candidate_features(
                candidate_row,
                filled_stock_info.get(candidate_code),
                financial_map.get(candidate_code),
                kline_map.get(candidate_code),
                resolved_similarity_type,
            )
            if not candidate_features:
                continue
            candidate_payloads.append({
                'code': candidate_code,
                'name': candidate_names.get(candidate_code, ''),
                'features': candidate_features,
            })

        feature_scales = _compute_feature_scales(target_features, candidate_payloads)
        similarities = []
        for payload in candidate_payloads:
            candidate_features = dict(payload.get('features') or {})
            common_features = set(target_features.keys()) & set(candidate_features.keys())
            if not common_features:
                continue
            distances = [
                abs(float(target_features[feature] or 0) - float(candidate_features[feature] or 0))
                / max(float(feature_scales.get(feature) or 1.0), 1e-6)
                for feature in common_features
            ]
            if not distances:
                continue
            avg_distance = statistics.mean(distances)
            similarity = 1 / (1 + avg_distance)
            similarities.append({
                'code': payload['code'],
                'name': payload['name'],
                'similarity': round(similarity, 4),
                'features': candidate_features,
            })

        # 5. 排序并返回
        similarities.sort(key=lambda x: x['similarity'], reverse=True)

        return propagate_data_quality_to_top(ok({
            'code': code,
            'name': target_info.get('name') or target_info.get('stock_name', ''),
            'industry': target_industry,
            'candidate_scope': candidate_scope,
            'similar_stocks': similarities[:top_n],
            'similarity_type': resolved_similarity_type,
            'total_candidates': len(candidate_rows),
            'calculated': len(similarities),
            'search_backend': backend_requested,
            'actual_backend': 'python',
            'allow_fallback': bool(allow_fallback),
            'backend_requested': backend_requested,
            'backend_used': 'python',
            'fallback_used': bool(backend_requested == 'db'),
            'fallback_reason': fallback_reason,
            'latency_ms': 0.0,
        }))

    except Exception as e:
        return fail(str(e))
