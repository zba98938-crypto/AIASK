from ._vector_common import *


def _robust_semantic_exclusion_terms(text):
    """Extract Chinese exclusion clauses without depending on file encoding."""
    import re as _re

    raw = str(text or "")
    terms = []
    pattern = (
        r'(?:\u4e0d\u8981|\u4e0d\u542b|\u5254\u9664|\u6392\u9664|'
        r'\u9664\u4e86|\u4f46\u4e0d\u8981)\s*([A-Za-z0-9\u4e00-\u9fff]{2,32})'
    )
    stop_tokens = [
        " ", "\t", "\n", "\r", ",", "\uff0c", "\u3002", ";", "\uff1b",
        "\u4e14", "\u5e76\u4e14", "\u540c\u65f6", "\u4f46", "\u7136\u540e",
        "\u800c\u4e14", "\u6216", "\u6216\u8005", "\u4ee5\u53ca",
        "rsi", "macd", "pe", "pb", "roe", "eps",
        "\u5e02\u76c8\u7387", "\u5e02\u51c0\u7387", "\u8d1f\u503a",
        "\u6362\u624b", "\u8425\u6536", "\u51c0\u5229\u6da6",
    ]
    for match in _re.finditer(pattern, raw, _re.IGNORECASE):
        term = str(match.group(1) or "").strip().lower()
        for stop in stop_tokens:
            idx = term.find(stop)
            if idx > 0:
                term = term[:idx].strip()
        if term:
            terms.append(term)
    return list(dict.fromkeys(terms))


def _robust_semantic_has_structured_conditions(text):
    """Detect structured screening hints such as RSI below 30."""
    import re as _re

    raw = str(text or "").lower()
    pattern = (
        r'(rsi|macd|pe|pb|roe|eps|'
        r'\u5e02\u76c8\u7387|\u5e02\u51c0\u7387|\u8d1f\u503a|\u6362\u624b|'
        r'\u8425\u6536|\u51c0\u5229\u6da6)'
        r'\s*(?:[<>]=?|\u4f4e\u4e8e|\u5c0f\u4e8e|\u9ad8\u4e8e|\u5927\u4e8e|'
        r'\u4e0d\u9ad8\u4e8e|\u4e0d\u4f4e\u4e8e)\s*\d+'
    )
    return bool(_re.search(pattern, raw))

async def semantic_stock_search(
    query: str,
    limit: int = 20
):
    """
    语义化股票搜索 - 基于关键词匹配（支持中文分词、行业关键词、股票代码/名称）

    Args:
        query: 搜索查询（支持股票代码、名称、行业关键词，如"白酒"、"新能源"、"贵州茅台"）
        limit: 返回数量
    """
    try:
        db = get_db()
        query_stripped = query.strip()
        if not query_stripped:
            return fail("查询关键词不能为空")

        # 检测财务指标条件表达式，如 "ROE>10"、"PE<20"、"净利润增速>15%"
        import re as _re_fin
        _FIN_METRIC_PATTERN = _re_fin.compile(
            r'(roe|pe|pb|eps|市盈率|市净率|净利润|营收|增速|负债率|股息率|市值|换手率)'
            r'\s*[><=!<>]{1,2}\s*[\d.]+',
            _re_fin.IGNORECASE,
        )
        if _FIN_METRIC_PATTERN.search(query_stripped):
            return ok({
                'query': query_stripped,
                'results': [],
                'count': 0,
                'hint': (
                    '检测到财务指标筛选条件（如 ROE>10、PE<20）。'
                    '本工具仅支持按股票代码、名称、行业关键词搜索；'
                    '财务条件筛选请使用 parse_selection_query 工具。'
                ),
                'suggestion': 'parse_selection_query',
            })

        # 生成搜索token列表:
        #   db_tokens: 2字及以上（用于DB LIKE，避免单字误匹配）
        #   sector_tokens: 含单字（用于行业名称模糊匹配）
        def _tokenize(text: str):
            import re as _re
            base = [text.lower()]
            cjk_chars = [c for c in text if '\u4e00' <= c <= '\u9fff']
            for i in range(len(cjk_chars) - 1):
                base.append(cjk_chars[i] + cjk_chars[i + 1])
            for seg in _re.findall(r'[\u4e00-\u9fff]{2,}', text):
                base.append(seg.lower())
            db_tks = list(dict.fromkeys(base))
            sector_tks = list(dict.fromkeys(base + cjk_chars))
            return db_tks, sector_tks

        tokens, sector_tokens = _tokenize(query_stripped)
        weighted_tokens = [t for t in tokens if len(t) >= 2]
        generic_tokens = [t for t in weighted_tokens if t in _GENERIC_SEMANTIC_HINTS]

        def _extract_exclusion_terms(text: str) -> List[str]:
            import re as _re_ex

            terms: List[str] = []
            for match in _re_ex.finditer(
                r'(?:不要|不含|剔除|排除|除了|但不要)\s*([A-Za-z0-9\u4e00-\u9fff]{2,12})',
                text,
            ):
                term = str(match.group(1) or '').strip().lower()
                if term:
                    terms.append(term)
            return list(dict.fromkeys(terms))

        def _has_structured_conditions(text: str) -> bool:
            import re as _re_cond

            return bool(_re_cond.search(
                r'(rsi|macd|pe|pb|roe|市盈率|市净率|负债|换手率|营收|净利润)\s*(?:[<>]=?|低于|小于|高于|大于|不高于|不低于)?\s*\d+',
                str(text or '').lower(),
            ))

        exclusion_terms = list(dict.fromkeys(
            _extract_exclusion_terms(query_stripped)
            + _robust_semantic_exclusion_terms(query_stripped)
        ))
        has_structured_conditions = (
            _has_structured_conditions(query_stripped)
            or _robust_semantic_has_structured_conditions(query_stripped)
        )

        def _extract_industry_query_tokens(text: str) -> List[str]:
            import re as _re3

            residual = text.lower()
            for hint in sorted(_GENERIC_SEMANTIC_HINTS, key=len, reverse=True):
                residual = residual.replace(hint, ' ')

            refined: List[str] = []
            for seg in _re3.findall(r'[\u4e00-\u9fff]{2,}', residual):
                refined.append(seg.lower())
                seg_tokens, _ = _tokenize(seg)
                refined.extend(
                    t for t in seg_tokens
                    if len(t) >= 2 and t != text.lower() and t not in _GENERIC_SEMANTIC_HINTS
                )
            return list(dict.fromkeys(refined))

        industry_query_tokens = _extract_industry_query_tokens(query_stripped) if generic_tokens else []

        async with db.acquire() as conn:
            code_col = await db._stocks_code_column(conn) if hasattr(db, "_stocks_code_column") else "code"
            try:
                stock_columns = await _fetch_table_columns(conn, 'stocks')
            except Exception:
                stock_columns = set()
            name_col = 'stock_name' if 'stock_name' in stock_columns else ('name' if 'name' in stock_columns else None)
            industry_col = 'industry' if 'industry' in stock_columns else ('sector' if 'sector' in stock_columns else None)
            market_cap_col = 'market_cap' if 'market_cap' in stock_columns else None
            pe_col = 'pe_ratio' if 'pe_ratio' in stock_columns else ('pe' if 'pe' in stock_columns else None)
            pb_col = 'pb_ratio' if 'pb_ratio' in stock_columns else ('pb' if 'pb' in stock_columns else None)
            select_name = f"{name_col} AS stock_name" if name_col else "'' AS stock_name"
            select_industry = f"{industry_col} AS industry" if industry_col else "'' AS industry"
            select_market_cap = f"{market_cap_col} AS market_cap" if market_cap_col else "NULL AS market_cap"
            select_pe = f"{pe_col} AS pe_ratio" if pe_col else "NULL AS pe_ratio"
            select_pb = f"{pb_col} AS pb_ratio" if pb_col else "NULL AS pb_ratio"
            seen_codes: set = set()
            all_rows: List[Dict] = []

            def _append_row(row: Dict):
                code = str(row.get('code', '') or '')
                if not code or code in seen_codes:
                    return
                seen_codes.add(code)
                all_rows.append({
                    'code': code,
                    'stock_name': row.get('stock_name') or row.get('name'),
                    'industry': row.get('industry'),
                    'market_cap': row.get('market_cap'),
                    'pe_ratio': row.get('pe_ratio'),
                    'pb_ratio': row.get('pb_ratio'),
                })

            def _has_industry_hits() -> bool:
                if not industry_query_tokens:
                    return False
                for row in all_rows:
                    industry_l = str(row.get('industry', '') or '').lower()
                    if any(token in industry_l for token in industry_query_tokens):
                        return True
                return False

            def _append_sector_candidates(match_text: str):
                try:
                    import akshare as ak_mod
                    from .market_blocks import _fetch_concept_stocks_from_ths

                    q_lower = match_text.lower()
                    strong_sector_tokens = [
                        token.lower()
                        for token in sector_tokens
                        if len(token) >= 2 and token not in _GENERIC_SEMANTIC_HINTS
                    ]

                    def _sector_match(name: str) -> bool:
                        name_l = name.lower()
                        if q_lower in name_l or name_l in q_lower:
                            return True
                        return any(token in name_l for token in strong_sector_tokens)

                    matched_sectors = []
                    seen_sector_keys = set()
                    candidate_limit = min(max(limit * 5, 50), 120)

                    def _append_sector(label: str, name: str):
                        label_s = str(label or '').strip()
                        name_s = str(name or '').strip()
                        if not label_s or not name_s:
                            return
                        key = (label_s, name_s)
                        if key in seen_sector_keys:
                            return
                        seen_sector_keys.add(key)
                        matched_sectors.append((label_s, name_s))

                    with suppress_stdout("[semantic_stock_search] stock_sector_spot"):
                        df_spot = ak_mod.stock_sector_spot()
                    if df_spot is not None and not df_spot.empty:
                        for _, srow in df_spot.iterrows():
                            bname = str(srow.get('板块', '') or '')
                            blabel = str(srow.get('label', '') or '')
                            if _sector_match(bname):
                                _append_sector(blabel, bname)
                    if len(matched_sectors) < 2:
                        with suppress_stdout("[semantic_stock_search] stock_board_industry_name_ths"):
                            df_ths = ak_mod.stock_board_industry_name_ths()
                        if df_ths is not None and not df_ths.empty:
                            for _, brow in df_ths.iterrows():
                                bname = str(brow.get('name', '') or '')
                                bcode = str(brow.get('code', '') or '')
                                if _sector_match(bname):
                                    _append_sector(bcode, bname)
                    if len(matched_sectors) < 3:
                        with suppress_stdout("[semantic_stock_search] stock_board_concept_name_ths"):
                            df_concept_ths = ak_mod.stock_board_concept_name_ths()
                        if df_concept_ths is not None and not df_concept_ths.empty:
                            for _, crow in df_concept_ths.iterrows():
                                bname = str(crow.get('name', '') or '')
                                bcode = str(crow.get('code', '') or '')
                                if _sector_match(bname):
                                    _append_sector(bcode, bname)
                    for blabel, bname in matched_sectors[:3]:
                        try:
                            if not blabel.startswith('new_'):
                                if blabel.isdigit() and len(blabel) == 6:
                                    with suppress_stdout("[semantic_stock_search] ths_concept_constituents"):
                                        concept_rows = _fetch_concept_stocks_from_ths(blabel, bname)
                                    for item in concept_rows[:candidate_limit]:
                                        _append_row({
                                            'code': str(item.get('stock_code', '') or ''),
                                            'name': str(item.get('stock_name', '') or ''),
                                            'industry': bname,
                                            'market_cap': None,
                                            'pe_ratio': None,
                                            'pb_ratio': None,
                                        })
                                continue
                            with suppress_stdout("[semantic_stock_search] stock_sector_detail"):
                                df_cons = ak_mod.stock_sector_detail(sector=blabel)
                            if df_cons is None or df_cons.empty:
                                continue
                            for _, crow in df_cons.head(candidate_limit).iterrows():
                                _append_row({
                                    'code': str(crow.get('code', '') or ''),
                                    'name': str(crow.get('name', '') or ''),
                                    'industry': bname,
                                    'market_cap': None,
                                    'pe_ratio': None,
                                    'pb_ratio': None,
                                })
                        except Exception:
                            continue
                except Exception:
                    pass

            # 对每个token做搜索（全量token中最多取前6个）
            for token in tokens[:6]:
                pat = f'%{token}%'
                rows = await conn.fetch(
                    f"""SELECT {code_col} AS code, {select_name}, {select_industry}, {select_market_cap}, {select_pe}, {select_pb}
                       FROM stocks
                       WHERE LOWER({code_col}) LIKE $1
                          OR ({'LOWER(' + name_col + ') LIKE $1' if name_col else '1 = 0'})
                          OR ({industry_col + ' IS NOT NULL AND LOWER(' + industry_col + ') LIKE $1' if industry_col else '1 = 0'})
                       ORDER BY {market_cap_col or code_col} DESC NULLS LAST
                       LIMIT $2""",
                    pat, limit * 2
                )
                for row in rows:
                    _append_row(dict(row))

            # 对“行业词 + 泛化词”查询补行业召回：即便已有名称命中，也继续尝试补全行业候选
            if industry_query_tokens and not _has_industry_hits():
                for token in industry_query_tokens[:4]:
                    rows = await conn.fetch(
                        f"""SELECT {code_col} AS code, {select_name}, {select_industry}, {select_market_cap}, {select_pe}, {select_pb}
                           FROM stocks
                           WHERE {industry_col + ' IS NOT NULL AND LOWER(' + industry_col + ') LIKE $1' if industry_col else '1 = 0'}
                           ORDER BY {market_cap_col or code_col} DESC NULLS LAST
                           LIMIT $2""",
                        f'%{token}%', limit * 2
                    )
                    for row in rows:
                        _append_row(dict(row))

            if industry_query_tokens and not _has_industry_hits():
                for token in industry_query_tokens[:3]:
                    for row in _search_stocks_tushare_fallback(token, limit * 2):
                        _append_row(row)

            if industry_query_tokens and not _has_industry_hits():
                for token in industry_query_tokens[:2]:
                    _append_sector_candidates(token)

            if not all_rows:
                _append_sector_candidates(query_stripped)
            query_lower = query_stripped.lower()
            for seed_key, seed_rows in _SEMANTIC_SECTOR_SEEDS.items():
                if seed_key.lower() in query_lower or query_lower in seed_key.lower():
                    for seed in seed_rows[: max(1, min(int(limit or 20), len(seed_rows)))]:
                        _append_row(seed)

            # 用 stocks 主表补齐市值/估值字段，帮助行业结果按龙头优先排序。
            missing_detail_codes = [
                row['code']
                for row in all_rows
                if row.get('market_cap') is None or row.get('pe_ratio') is None or row.get('pb_ratio') is None
            ]
            if missing_detail_codes:
                detail_codes = list(dict.fromkeys(missing_detail_codes))
                placeholders = ", ".join(f"${idx + 1}" for idx in range(len(detail_codes)))
                detail_rows = await conn.fetch(
                    f"""SELECT {code_col} AS code, {select_market_cap}, {select_pe}, {select_pb}
                       FROM stocks
                       WHERE {code_col} IN ({placeholders})""",
                    *detail_codes
                )
                detail_map = {str(row['code']): dict(row) for row in detail_rows}
                for row in all_rows:
                    detail = detail_map.get(str(row.get('code') or ''))
                    if not detail:
                        continue
                    if row.get('market_cap') is None:
                        row['market_cap'] = detail.get('market_cap')
                    if row.get('pe_ratio') is None:
                        row['pe_ratio'] = detail.get('pe_ratio')
                    if row.get('pb_ratio') is None:
                        row['pb_ratio'] = detail.get('pb_ratio')

            # 计算匹配分数
            results = []
            query_explicitly_conceptual = any(hint in query_stripped for hint in _SEMANTIC_CONCEPT_HINTS)
            semantic_seed_codes = {
                str(seed.get('code') or '')
                for seed_key, seed_rows in _SEMANTIC_SECTOR_SEEDS.items()
                if seed_key.lower() in query_stripped.lower() or query_stripped.lower() in seed_key.lower()
                for seed in seed_rows
            }
            for row in all_rows:
                score = 0.0
                match_type = []
                code_l = (row['code'] or '').lower()
                name_l = (row['stock_name'] or '').lower()
                industry_l = (row['industry'] or '').lower()
                q_l = query_stripped.lower()
                if exclusion_terms and any(term and (term in code_l or term in name_l) for term in exclusion_terms):
                    continue
                is_st = _semantic_is_special_treatment(row.get('stock_name'))
                is_concept = _semantic_is_concept_industry(row.get('industry'))
                matched_name_tokens = [t for t in weighted_tokens if t in name_l]
                matched_industry_tokens = [t for t in weighted_tokens if t in industry_l]
                strong_name_tokens = [t for t in matched_name_tokens if t not in _GENERIC_SEMANTIC_HINTS]
                generic_name_tokens = [t for t in matched_name_tokens if t in _GENERIC_SEMANTIC_HINTS]
                strong_industry_tokens = [t for t in matched_industry_tokens if t not in _GENERIC_SEMANTIC_HINTS]

                if code_l == q_l:
                    score += 1.0; match_type.append('code_exact')
                elif q_l in code_l:
                    score += 0.8; match_type.append('code_partial')
                if q_l in name_l:
                    score += 0.9; match_type.append('name_exact')
                elif strong_name_tokens:
                    score += 0.6; match_type.append('name_partial')
                elif generic_name_tokens:
                    score += 0.2; match_type.append('name_hint')
                if industry_l and strong_industry_tokens:
                    if is_concept and not query_explicitly_conceptual:
                        score += 0.6; match_type.append('industry_concept')
                    else:
                        score += 0.85; match_type.append('industry')
                    if generic_tokens:
                        score += 0.2; match_type.append('industry_context')
                elif industry_l and matched_industry_tokens:
                    score += 0.45; match_type.append('industry_hint')
                if is_concept and not query_explicitly_conceptual:
                    score -= 0.1; match_type.append('concept_penalty')
                if is_st:
                    score -= 0.9; match_type.append('special_treatment_penalty')
                if row.get('code') in semantic_seed_codes:
                    score += 1.0; match_type.append('sector_seed')
                if score == 0:
                    continue
                if score < _MIN_SEMANTIC_SCORE and not {'code_exact', 'code_partial', 'name_exact'} & set(match_type):
                    continue

                results.append({
                    'code': row['code'],
                    'name': row['stock_name'],
                    'industry': row['industry'],
                    'market_cap': float(row['market_cap']) if row.get('market_cap') else None,
                    'pe_ratio': float(row['pe_ratio']) if row.get('pe_ratio') else None,
                    'pb_ratio': float(row['pb_ratio']) if row.get('pb_ratio') else None,
                    'score': round(score, 2),
                    'match_type': match_type,
                })

            results.sort(
                key=lambda x: (
                    x['score'],
                    x['market_cap'] if x.get('market_cap') is not None else float('-inf'),
                ),
                reverse=True,
            )

            final_results = results[:limit]
            response_payload = {
                'query': query_stripped,
                'results': final_results,
                'count': len(final_results),
            }
            if exclusion_terms:
                response_payload['excluded_terms'] = exclusion_terms
            if has_structured_conditions:
                response_payload['suggestion'] = 'parse_selection_query'
                response_payload['condition_hint'] = (
                    '检测到 RSI/估值/财务等结构化筛选条件；semantic_stock_search 只做语义候选召回，'
                    '请用 parse_selection_query + screener_manager 执行条件筛选。'
                )
            if not final_results:
                response_payload['hint'] = (
                    f'未找到与"{query_stripped}"匹配的股票。'
                    '建议尝试：行业名称（如"白酒"、"新能源"）、股票名称或代码；'
                    '若要按财务指标筛选请使用 parse_selection_query 工具。'
                )
            return ok(response_payload)

    except Exception as e:
        return fail(str(e))
