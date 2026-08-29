"""新闻/研报工具 — 研报、分析师排名、盈利预测"""

from datetime import date, timedelta

try:
    import akshare as ak
except ImportError:
    ak = None

from ...services.db_first_market_context import load_db_first_document_context
from ...storage import get_db
from ...core.cache_manager import cached
from ...core.rate_limiter import get_limiter
from ...data_source import data_source
from ...utils import (
    attach_argument_contract_meta,
    fail,
    format_period,
    propagate_data_quality_to_top,
    normalize_code,
    ok,
    parse_date_input,
    pick_value,
    resolve_canonical_arg,
    resolve_existing_security_code_sync,
    safe_float,
    validate_int_range,
)
from ..data_quality import build_quality_meta
from ..fund_flow_common import _run_storage_call_sync
from .helpers import _dedup_reports, _fetch_eastmoney_research


def _empty_degraded_response(data: dict, *, fallback_reason: str, source_chain: list[str]) -> dict:
    response = ok(data)
    response.update(
        build_quality_meta(
            source="none",
            source_chain=source_chain,
            fallback_reason=fallback_reason,
            asof_value=date.today().isoformat(),
            missing_fields=[],
            degraded=True,
            success=True,
            accepted_count=0,
            rejected_count=0,
        )
    )
    return propagate_data_quality_to_top(response)


def _iter_keyword_bigrams(keyword: str) -> list[str]:
    text = str(keyword or "").strip().lower()
    if len(text) < 2:
        return []
    return list(dict.fromkeys(text[i:i + 2] for i in range(len(text) - 1)))


def _research_candidate_match(text: str, keyword: str, match_fn) -> bool:
    if match_fn(text, keyword):
        return True
    text_lower = str(text or "").lower()
    if len(keyword or "") < 4:
        return False
    return any(token in text_lower for token in _iter_keyword_bigrams(keyword))


def _filter_recent_reports(reports: list[dict], since: date, limit: int = 20) -> list[dict]:
    filtered: list[dict] = []
    for report in reports:
        report_date = parse_date_input(report.get("date"))
        if report_date is None or report_date < since:
            continue
        normalized = dict(report)
        normalized["date"] = report_date.isoformat()
        filtered.append(normalized)
    filtered.sort(
        key=lambda item: (
            item.get("date", ""),
            str(item.get("title", "")),
            str(item.get("institution", "")),
        ),
        reverse=True,
    )
    return _dedup_reports(filtered)[:limit]


def _search_research_candidate_codes(keyword: str, limit: int, match_fn) -> list[dict]:
    keyword_stripped = str(keyword or "").strip()
    if not keyword_stripped or limit <= 0:
        return []

    try:
        pro = data_source.get_tushare_pro()
        if not pro:
            return []
        df = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,industry",
        )
        if df is None or df.empty:
            return []
        candidates: list[dict] = []
        seen_codes: set[str] = set()
        for _, row in df.iterrows():
            symbol = str(row.get("symbol", "") or "")
            code = normalize_code(symbol) if symbol else normalize_code(row.get("ts_code", ""))
            if not code or code in seen_codes:
                continue
            name = str(row.get("name", "") or "").strip()
            industry = str(row.get("industry", "") or "").strip()
            if not any(
                _research_candidate_match(field, keyword_stripped, match_fn)
                for field in (code, name, industry)
            ):
                continue
            seen_codes.add(code)
            candidates.append(
                {
                    "code": code,
                    "name": name,
                    "industry": industry,
                }
            )
            if len(candidates) >= limit:
                break
        return candidates
    except Exception:
        return []


@cached(ttl=3600.0)
def get_stock_research(
    code: str = "",
    limit: int = 10,
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
) -> dict:
    """
    获取个股研究报告列表

    数据源优先级: Tushare report_rc → 东财 datacenter → AkShare (降级)
    时效性: 研报数据缓存1小时
    """
    limiter = get_limiter("news", rate=3.0)
    limiter.acquire()

    try:
        raw_code, alias_hits, _ = resolve_canonical_arg(
            "code",
            code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        resolved_limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1, maximum=100)
        canonical_args = {"code": normalize_code(raw_code) if raw_code else "", "limit": resolved_limit}
        if not raw_code:
            return attach_argument_contract_meta(
                fail("需要提供股票代码（支持 code / stock_code / symbol / ticker）"),
                canonical_tool="get_stock_research",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )
        if limit_error:
            return attach_argument_contract_meta(
                fail(limit_error),
                canonical_tool="get_stock_research",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )
        code = normalize_code(raw_code)
        canonical_args["code"] = code

        # 1. 主路径: Tushare report_rc
        try:
            ts_pro = data_source.get_tushare_pro()
            if ts_pro:
                ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                df_ts = ts_pro.report_rc(ts_code=ts_code, fields='report_title,org_name,author_name,report_date,rating')
                if df_ts is not None and not df_ts.empty:
                    reports = []
                    for _, row in df_ts.iterrows():
                        reports.append({
                            "title": str(row.get('report_title', '') or '').strip(),
                            "institution": str(row.get('org_name', '') or '').strip(),
                            "author": str(row.get('author_name', '') or '').strip(),
                            "rating": str(row.get('rating', '') or '').strip(),
                            "targetPrice": None,
                            "date": format_period(row.get('report_date')),
                        })
                    reports = _dedup_reports(reports)[:resolved_limit]
                    if reports:
                        return attach_argument_contract_meta(
                            ok({"stockCode": code, "reports": reports, "total": len(reports)}),
                            canonical_tool="get_stock_research",
                            canonical_args=canonical_args,
                            alias_hits=alias_hits,
                        )
        except Exception:
            pass

        # 2. 降级: 东财 datacenter 研报
        try:
            items = _fetch_eastmoney_research(code, resolved_limit * 2)
            if items:
                items = _dedup_reports(items)[:resolved_limit]
                return attach_argument_contract_meta(
                    ok({"stockCode": code, "reports": items, "total": len(items)}),
                    canonical_tool="get_stock_research",
                    canonical_args=canonical_args,
                    alias_hits=alias_hits,
                )
        except Exception:
            pass

        # 3. 降级: AkShare
        if ak is not None:
            try:
                df = ak.stock_research_report_em(symbol=code)
                if df is not None and not df.empty:
                    reports = []
                    for _, row in df.iterrows():
                        report = {
                            "title": str(pick_value(row, ["报告名称", "标题", "研报标题", "title"]) or "").strip(),
                            "institution": str(pick_value(row, ["机构名称", "机构", "研究机构", "发布机构", "institution"]) or "").strip(),
                            "author": str(pick_value(row, ["研究员", "作者", "分析师", "author"]) or "").strip(),
                            "rating": str(pick_value(row, ["最新评级", "评级", "投资评级", "rating"]) or "").strip(),
                            "targetPrice": safe_float(pick_value(row, ["目标价", "最高目标价", "目标价格", "targetPrice"])),
                            "date": format_period(pick_value(row, ["发布日期", "日期", "发布时间", "date"])),
                        }
                        if report["title"] or report["institution"]:
                            reports.append(report)
                    reports = _dedup_reports(reports)[:resolved_limit]
                    if reports:
                        return attach_argument_contract_meta(
                            ok({"stockCode": code, "reports": reports, "total": len(reports)}),
                            canonical_tool="get_stock_research",
                            canonical_args=canonical_args,
                            alias_hits=alias_hits,
                        )
            except Exception:
                pass

        return attach_argument_contract_meta(
            _empty_degraded_response(
                {"stockCode": code, "reports": [], "total": 0, "message": f"暂无股票 {code} 的研报数据"},
                fallback_reason=f"暂无股票 {code} 的研报数据",
                source_chain=["tushare.report_rc", "eastmoney.research", "akshare.stock_research_report_em"],
            ),
            canonical_tool="get_stock_research",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )
    except Exception as e:
        return attach_argument_contract_meta(
            fail(e),
            canonical_tool="get_stock_research",
            canonical_args={"code": normalize_code(code) if code else "", "limit": limit},
            alias_hits=[],
        )


@cached(ttl=3600.0)
def search_research(keyword: str = "", stock_code: str = "", days: int = 90) -> dict:
    """
    搜索研究报告

    数据源优先级: Tushare report_rc → 东财 datacenter → AkShare (降级)
    时效性: 研报数据缓存1小时
    """
    limiter = get_limiter("news", rate=3.0)
    limiter.acquire()

    def _keyword_match(text: str, kw: str) -> bool:
        text_lower = str(text or "").lower()
        kw_lower = kw.lower()
        if kw_lower in text_lower:
            return True
        if len(kw_lower) >= 4:
            tokens = set()
            for i in range(len(kw_lower) - 1):
                tokens.add(kw_lower[i:i+2])
            hit = sum(1 for t in tokens if t in text_lower)
            if hit >= max(1, len(tokens) // 2):
                return True
        return False

    try:
        days, days_error = validate_int_range(days, field_name="days", minimum=1, maximum=3650)
        if days_error:
            return fail(days_error)
        since = date.today() - timedelta(days=days)

        code = ""
        if stock_code:
            code, _, error = resolve_existing_security_code_sync(stock_code=stock_code)
            if error:
                return fail(error)

        # 1. 主路径: Tushare report_rc
        try:
            ts_pro = data_source.get_tushare_pro()
            if ts_pro:
                end_dt = date.today().strftime("%Y%m%d")
                start_dt = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
                ts_kwargs = {"start_date": start_dt, "end_date": end_dt, "fields": "ts_code,report_title,org_name,author_name,report_date,rating"}
                if code:
                    ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                    ts_kwargs["ts_code"] = ts_code
                df_ts = ts_pro.report_rc(**ts_kwargs)

                # 若仅有关键词但无 stock_code，且 Tushare 结果过少或无结果，
                # 先通过 stock_basic 将关键词解析成股票代码再精确拉取
                if keyword and not code and (df_ts is None or df_ts.empty):
                    try:
                        df_basic = ts_pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name")
                        if df_basic is not None and not df_basic.empty:
                            matched_ts_codes = [
                                str(r.get("ts_code", ""))
                                for _, r in df_basic.iterrows()
                                if _keyword_match(str(r.get("name", "")), keyword) or keyword in str(r.get("symbol", ""))
                            ]
                            for ts_c in matched_ts_codes[:3]:
                                try:
                                    df_rc = ts_pro.report_rc(ts_code=ts_c, start_date=start_dt, end_date=end_dt,
                                                              fields="ts_code,report_title,org_name,author_name,report_date,rating")
                                    if df_rc is not None and not df_rc.empty:
                                        df_ts = df_rc if df_ts is None or df_ts.empty else df_ts._append(df_rc, ignore_index=True)
                                except Exception:
                                    pass
                    except Exception:
                        pass

                if df_ts is not None and not df_ts.empty:
                    if keyword:
                        df_ts = df_ts[
                            df_ts.apply(
                                lambda row: _keyword_match(row.get("report_title"), keyword)
                                or _keyword_match(row.get("org_name"), keyword),
                                axis=1
                            )
                        ]
                    reports = []
                    for _, row in df_ts.iterrows():
                        ts_c = str(row.get("ts_code", "") or "").split(".")[0]
                        reports.append({
                            "stockCode": ts_c or code,
                            "title": str(row.get("report_title", "") or "").strip(),
                            "institution": str(row.get("org_name", "") or "").strip(),
                            "rating": str(row.get("rating", "") or "").strip(),
                            "date": format_period(row.get("report_date")),
                        })
                    reports = _filter_recent_reports(reports, since)
                    if reports:
                        return ok({"keyword": keyword, "stockCode": code, "reports": reports, "total": len(reports)})
        except Exception:
            pass

        # 2. 降级: 东财 datacenter
        if code:
            try:
                items = _fetch_eastmoney_research(code, 20)
                if items and keyword:
                    items = [i for i in items if _keyword_match(i.get("title", ""), keyword) or _keyword_match(i.get("institution", ""), keyword)]
                if items:
                    reports = _filter_recent_reports([{"stockCode": code, **i} for i in items], since)
                    return ok({"keyword": keyword, "stockCode": code, "reports": reports, "total": len(reports)})
            except Exception:
                pass

        # 3. 降级: AkShare
        if code and ak is not None:
            try:
                df = ak.stock_research_report_em(symbol=code)
                if df is not None and not df.empty:
                    if keyword:
                        df = df[
                            df.apply(
                                lambda row: _keyword_match(pick_value(row, ["报告名称", "标题", "研报标题", "title"]), keyword)
                                or _keyword_match(pick_value(row, ["机构名称", "机构", "研究机构", "institution"]), keyword),
                                axis=1
                            )
                        ]
                    reports = []
                    for _, row in df.iterrows():
                        reports.append({
                            "stockCode": code,
                            "title": str(pick_value(row, ["报告名称", "标题", "研报标题", "title"]) or "").strip(),
                            "institution": str(pick_value(row, ["机构名称", "机构", "研究机构", "发布机构", "institution"]) or "").strip(),
                            "rating": str(pick_value(row, ["最新评级", "评级", "投资评级", "rating"]) or "").strip(),
                            "date": format_period(pick_value(row, ["发布日期", "日期", "发布时间", "date"])),
                        })
                    reports = _filter_recent_reports(reports, since)
                    if reports:
                        return ok({"keyword": keyword, "stockCode": code, "reports": reports, "total": len(reports)})
            except Exception:
                pass
        elif keyword:
            try:
                candidate_codes = _search_research_candidate_codes(keyword, 5, _keyword_match)
                fallback_reports = []
                per_code_limit = max(3, min(6, 20 // max(len(candidate_codes), 1)))
                for candidate in candidate_codes:
                    items = _fetch_eastmoney_research(candidate["code"], per_code_limit)
                    if not items and ak is not None:
                        try:
                            df = ak.stock_research_report_em(symbol=candidate["code"])
                            if df is not None and not df.empty:
                                items = []
                                for _, row in df.head(per_code_limit).iterrows():
                                    items.append({
                                        "title": str(pick_value(row, ["报告名称", "标题", "研报标题", "title"]) or "").strip(),
                                        "institution": str(pick_value(row, ["机构名称", "机构", "研究机构", "发布机构", "institution"]) or "").strip(),
                                        "author": str(pick_value(row, ["研究员", "作者", "分析师", "author"]) or "").strip(),
                                        "rating": str(pick_value(row, ["最新评级", "评级", "投资评级", "rating"]) or "").strip(),
                                        "date": format_period(pick_value(row, ["发布日期", "日期", "发布时间", "date"])),
                                    })
                        except Exception:
                            items = []
                    for item in items:
                        match_score = 2 if (
                            _keyword_match(item.get("title", ""), keyword)
                            or _keyword_match(item.get("institution", ""), keyword)
                        ) else 1
                        fallback_reports.append(
                            {
                                "stockCode": candidate["code"],
                                "stockName": candidate["name"],
                                "title": item.get("title", ""),
                                "institution": item.get("institution", ""),
                                "rating": item.get("rating", ""),
                                "date": item.get("date", ""),
                                "_match_score": match_score,
                            }
                        )

                if fallback_reports:
                    fallback_reports.sort(key=lambda item: item.get("_match_score", 0), reverse=True)
                    reports = []
                    for report in _filter_recent_reports(fallback_reports, since):
                        report.pop("_match_score", None)
                        reports.append(report)
                    if reports:
                        return ok(
                            {
                                "keyword": keyword,
                                "stockCode": "",
                                "reports": reports[:20],
                                "total": len(reports[:20]),
                                "message": "已使用候选股票研报作为关键词检索补充结果",
                            }
                        )
            except Exception:
                pass

        # 终极降级: 查 DB research_reports 表（与 search_research_db 同源）
        try:
            db = get_db()
            since = date.today() - timedelta(days=days)
            conditions = ["publish_date >= $1"]
            params: list = [since]
            idx = 2
            if code:
                conditions.append(f"code = ${idx}")
                params.append(code)
                idx += 1
            if keyword:
                conditions.append(f"(title ILIKE ${idx} OR summary ILIKE ${idx})")
                params.append(f"%{keyword.strip()}%")
                idx += 1
            where_clause = " AND ".join(conditions)
            query = f"SELECT * FROM research_reports WHERE {where_clause} ORDER BY publish_date DESC LIMIT 20"

            async def _fetch_db():
                async with db.acquire() as conn:
                    return await conn.fetch(query, *params)

            rows = _run_storage_call_sync(_fetch_db)
            if rows:
                reports = []
                for r in rows:
                    r = dict(r)
                    reports.append({
                        "stockCode": str(r.get("code") or code or ""),
                        "title": str(r.get("title") or "").strip(),
                        "institution": str(r.get("institution") or "").strip(),
                        "rating": str(r.get("rating") or "").strip(),
                        "date": format_period(r.get("publish_date")),
                    })
                reports = _filter_recent_reports(reports, since)
                if reports:
                    return ok({
                        "keyword": keyword, "stockCode": code,
                        "reports": reports, "total": len(reports),
                        "message": "结果来自数据库研报缓存",
                    })
        except Exception:
            pass

        return ok({"keyword": keyword, "stockCode": code, "reports": [], "total": 0, "message": "未找到匹配的研报"})
    except Exception as e:
        return fail(e)


@cached(ttl=86400.0)
def get_analyst_ranking(year: str = "") -> dict:
    """
    获取分析师排名（基于研报数量统计）

    数据源: Tushare report_rc
    时效性: 缓存24小时
    """
    limiter = get_limiter("news", rate=3.0)
    limiter.acquire()

    try:
        if not year:
            year = str(date.today().year)

        try:
            ts_pro = data_source.get_tushare_pro()
            if ts_pro:
                target_year = int(year)
                df = None
                actual_year = None
                for try_year in range(target_year, target_year - 5, -1):
                    try:
                        start_date = f"{try_year}0101"
                        end_date = f"{try_year}1231"
                        df = ts_pro.report_rc(
                            start_date=start_date,
                            end_date=end_date,
                            fields='author_name,org_name,report_date'
                        )
                        if df is not None and not df.empty:
                            actual_year = str(try_year)
                            break
                    except Exception:
                        continue

                if df is not None and not df.empty:
                    analyst_counts = df.groupby(['author_name', 'org_name']).size().reset_index(name='report_count')
                    analyst_counts = analyst_counts.sort_values('report_count', ascending=False).head(50)

                    analysts = []
                    for rank, (_, row) in enumerate(analyst_counts.iterrows(), 1):
                        name = str(row.get('author_name', '') or '').strip()
                        institution = str(row.get('org_name', '') or '').strip()
                        if not name:
                            continue
                        analysts.append({
                            'rank': rank,
                            'name': name,
                            'institution': institution,
                            'industry': '',
                            'score': None,
                            'winRate': None,
                            'report_count': int(row.get('report_count', 0)),
                        })

                    if analysts:
                        note = '基于研报数量统计的活跃分析师排名'
                        if actual_year and actual_year != year:
                            note += f'（{year}年数据不足，使用{actual_year}年数据）'
                        return ok({
                            'year': actual_year or year,
                            'requested_year': year,
                            'analysts': analysts,
                            'total': len(analysts),
                            'source': 'tushare_report_rc',
                            'note': note,
                        })
        except Exception:
            pass

        # P3-5.20 fix: empty analysts → degraded=true(诊断报告 §5.20)
        # 历史问题:返回 success=true / fallback_used=false / degraded=false 但 analysts=[]
        # AI 误以为今年无人活跃,实际是数据源限制
        return ok({
            'year': year,
            'analysts': [],
            'total': 0,
            'message': f"暂无 {year} 年分析师排名数据（数据源限制）",
            'degraded': True,
            'fallback_used': True,
            'fallback_reason': 'tushare_report_rc_no_data_for_recent_5_years',
            'quality_flags': ['empty_upstream', 'data_source_limitation'],
        })
    except Exception as e:
        return fail(e)


@cached(ttl=3600.0)
def get_research_reports(
    code: str = "",
    stock_code: str = "",
    symbol: str = "",
    limit: int = 10,
    *,
    ticker: str = "",
    prefer_db: bool = True,
) -> dict:
    """
    获取个股研报

    数据源优先级: AkShare 东财研报 → Tushare report_rc → 东财 datacenter
    时效性: 缓存1小时

    Args:
        symbol: 股票代码（6位）
        stock_code: 股票代码别名（与 symbol 等价）
        limit: 返回条数
    """
    limiter = get_limiter("news", rate=3.0)
    limiter.acquire()

    try:
        raw, alias_hits, _ = resolve_canonical_arg(
            "code",
            code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        code = normalize_code(raw) if raw else ""
        resolved_limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1, maximum=100)
        canonical_args = {"code": code, "limit": resolved_limit}
        if limit_error:
            return attach_argument_contract_meta(
                fail(limit_error),
                canonical_tool="get_research_reports",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )

        if prefer_db and code:
            try:
                db_context, db_source_chain = _run_storage_call_sync(
                    lambda: load_db_first_document_context(
                        get_db(),
                        code,
                        research_limit=max(resolved_limit, 1),
                    ),
                    timeout=8.0,
                )
                db_reports = list((db_context or {}).get("research") or [])
                if db_reports:
                    return attach_argument_contract_meta(
                        ok(
                            {
                                "stockCode": code,
                                "reports": _dedup_reports(db_reports)[:resolved_limit],
                                "total": min(len(db_reports), resolved_limit),
                                "source": ",".join(db_source_chain or ["db.research_reports"]),
                            }
                        ),
                        canonical_tool="get_research_reports",
                        canonical_args=canonical_args,
                        alias_hits=alias_hits,
                    )
            except Exception:
                pass

        # 1. AkShare 东财研报（支持 per-stock 过滤，字段完整）
        if ak is not None and code:
            try:
                df = ak.stock_research_report_em(symbol=code)
                if df is not None and not df.empty:
                    records = []
                    for _, row in df.head(resolved_limit * 2).iterrows():
                        rec_code = str(row.get("股票代码", "") or "").strip()
                        if rec_code and normalize_code(rec_code) != code:
                            continue
                        records.append({
                            "title": str(row.get("报告名称", "") or "").strip(),
                            "institution": str(row.get("机构", "") or "").strip(),
                            "author": "",
                            "rating": str(row.get("东财评级", "") or "").strip(),
                            "targetPrice": None,
                            "date": format_period(row.get("日期")),
                        })
                    records = _dedup_reports(records)[:resolved_limit]
                    if records:
                        return attach_argument_contract_meta(
                            ok({"stockCode": code, "reports": records, "total": len(records)}),
                            canonical_tool="get_research_reports",
                            canonical_args=canonical_args,
                            alias_hits=alias_hits,
                        )
            except Exception:
                pass

        # 2. Tushare report_rc（需要日期范围才能有效过滤）
        try:
            ts_pro = data_source.get_tushare_pro()
            if ts_pro:
                from datetime import date as _date, timedelta as _td
                end_dt = _date.today().strftime("%Y%m%d")
                start_dt = (_date.today() - _td(days=365)).strftime("%Y%m%d")
                kwargs = {
                    "fields": "ts_code,report_title,org_name,author_name,report_date,rating",
                    "start_date": start_dt,
                    "end_date": end_dt,
                }
                if code:
                    ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                    kwargs["ts_code"] = ts_code
                df_ts = ts_pro.report_rc(**kwargs)
                if df_ts is not None and not df_ts.empty:
                    records = []
                    for _, row in df_ts.head(resolved_limit).iterrows():
                        records.append({
                            "title": str(row.get("report_title", "") or "").strip(),
                            "institution": str(row.get("org_name", "") or "").strip(),
                            "author": str(row.get("author_name", "") or "").strip(),
                            "rating": str(row.get("rating", "") or "").strip(),
                            "date": format_period(row.get("report_date")),
                        })
                    records = _dedup_reports(records)[:resolved_limit]
                    if records:
                        return attach_argument_contract_meta(
                            ok({"stockCode": code, "reports": records, "total": len(records)}),
                            canonical_tool="get_research_reports",
                            canonical_args=canonical_args,
                            alias_hits=alias_hits,
                        )
        except Exception:
            pass

        # 3. 东财 datacenter（RPT_RATINGCHANGE_DET）
        if code:
            items = _fetch_eastmoney_research(code, resolved_limit * 2)
            if items:
                return attach_argument_contract_meta(
                    ok({"stockCode": code, "reports": _dedup_reports(items)[:resolved_limit], "total": len(items)}),
                    canonical_tool="get_research_reports",
                    canonical_args=canonical_args,
                    alias_hits=alias_hits,
                )

        return attach_argument_contract_meta(
            _empty_degraded_response(
                {"stockCode": code or raw, "reports": [], "total": 0, "message": f"未找到股票 {code or raw} 的研报数据"},
                fallback_reason=f"未找到股票 {code or raw} 的研报数据",
                source_chain=["db.research_reports", "akshare.stock_research_report_em", "tushare.report_rc", "eastmoney.research"],
            ),
            canonical_tool="get_research_reports",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )
    except Exception as e:
        return attach_argument_contract_meta(
            fail(e),
            canonical_tool="get_research_reports",
            canonical_args={"code": normalize_code(code) if code else "", "limit": limit},
            alias_hits=[],
        )


@cached(ttl=3600.0)
def get_profit_forecast(
    code: str = "",
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
) -> dict:
    """
    获取个股盈利预测（包含机构评级和目标价）

    数据源优先级: 东财 datacenter → Tushare forecast → AkShare (降级)
    时效性: 缓存1小时
    """
    limiter = get_limiter("news", rate=3.0)
    limiter.acquire()

    try:
        raw_code, alias_hits, _ = resolve_canonical_arg(
            "code",
            code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        code = normalize_code(raw_code)
        canonical_args = {"code": code}
        if not code:
            return attach_argument_contract_meta(
                fail("需要提供股票代码（支持 code / stock_code / symbol / ticker）"),
                canonical_tool="get_profit_forecast",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )

        # 1. 东财 datacenter 盈利预测
        try:
            import requests as _req
            url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
            params = {
                "sortColumns": "REPORT_DATE",
                "sortTypes": -1,
                "pageSize": 30,
                "pageNumber": 1,
                "reportName": "RPT_PUBLIC_OP_NEWPREDICT",
                "columns": "REPORT_DATE,ORG_NAME,RESEARCHER,RATING_NAME,PREDICT_NEXT_TWO_EPS,PREDICT_NEXT_TWO_INCOME,PREDICT_NEXT_TWO_NETPROFIT",
                "filter": f'(SECURITY_CODE="{code}")',
            }
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
            resp = _req.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                payload = resp.json()
                items = payload.get("result", {}).get("data", []) or []
                if items:
                    records = []
                    for item in items:
                        records.append({
                            "date": str(item.get("REPORT_DATE", "")).split(" ")[0],
                            "institution": str(item.get("ORG_NAME") or ""),
                            "researcher": str(item.get("RESEARCHER") or ""),
                            "rating": str(item.get("RATING_NAME") or ""),
                            "eps_forecast": safe_float(item.get("PREDICT_NEXT_TWO_EPS")),
                            "income_forecast": safe_float(item.get("PREDICT_NEXT_TWO_INCOME")),
                            "netprofit_forecast": safe_float(item.get("PREDICT_NEXT_TWO_NETPROFIT")),
                        })
                    return attach_argument_contract_meta(
                        ok({"stockCode": code, "items": records, "total": len(records)}),
                        canonical_tool="get_profit_forecast",
                        canonical_args=canonical_args,
                        alias_hits=alias_hits,
                    )
        except Exception:
            pass

        # 2. Tushare forecast
        try:
            ts_pro = data_source.get_tushare_pro()
            if ts_pro:
                ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                df_ts = ts_pro.forecast(ts_code=ts_code)
                if df_ts is not None and not df_ts.empty:
                    records = df_ts.head(20).fillna("").to_dict(orient="records")
                    return attach_argument_contract_meta(
                        ok({"stockCode": code, "items": records, "total": len(records)}),
                        canonical_tool="get_profit_forecast",
                        canonical_args=canonical_args,
                        alias_hits=alias_hits,
                    )
        except Exception:
            pass

        # 3. AkShare
        if ak is not None:
            df = None
            try:
                df = ak.stock_profit_forecast_em(symbol=code)
            except Exception:
                pass
            if df is None or df.empty:
                try:
                    df = ak.stock_profit_forecast_ths(symbol=code)
                except Exception:
                    pass
            if df is not None and not df.empty:
                try:
                    records = df.fillna("").to_dict(orient="records")
                    return attach_argument_contract_meta(
                        ok({"stockCode": code, "items": records, "total": len(records)}),
                        canonical_tool="get_profit_forecast",
                        canonical_args=canonical_args,
                        alias_hits=alias_hits,
                    )
                except Exception:
                    pass

        # P3-5.21 fix: 第四源 — 从研报正文回填盈利预测(诊断报告 §5.21)
        # 历史问题:东财/Tushare/AkShare 三源全跪时 items=[],AI 没有任何 fallback
        # 修复:从 stock_research_report_em / stock_research_report_ths 的研报里抽取近期评级和目标价作为简化 forecast
        if ak is not None:
            for fn_name in ('stock_research_report_em', 'stock_research_report_ths'):
                fn = getattr(ak, fn_name, None)
                if not callable(fn):
                    continue
                try:
                    df_research = fn(symbol=code)
                except Exception:
                    continue
                if df_research is None or df_research.empty:
                    continue
                try:
                    head_df = df_research.head(20).fillna("")
                    extracted: list[dict] = []
                    for _, row in head_df.iterrows():
                        rating_col = next((col for col in row.index if '评级' in str(col)), None)
                        org_col = next((col for col in row.index if '机构' in str(col) or 'org' in str(col).lower()), None)
                        date_col = next((col for col in row.index if '日期' in str(col) or 'date' in str(col).lower()), None)
                        researcher_col = next((col for col in row.index if '分析师' in str(col)), None)
                        target_col = next((col for col in row.index if '目标价' in str(col)), None)
                        eps_col = next((col for col in row.index if 'eps' in str(col).lower() or '每股' in str(col)), None)
                        extracted.append({
                            "date": str(row.get(date_col, "") if date_col else ""),
                            "institution": str(row.get(org_col, "") if org_col else ""),
                            "researcher": str(row.get(researcher_col, "") if researcher_col else ""),
                            "rating": str(row.get(rating_col, "") if rating_col else ""),
                            "target_price": safe_float(row.get(target_col, None) if target_col else None),
                            "eps_forecast": safe_float(row.get(eps_col, None) if eps_col else None),
                            "_source": fn_name,
                            "_synthesized_from_research_report": True,
                        })
                    if extracted:
                        return attach_argument_contract_meta(
                            ok({
                                "stockCode": code,
                                "items": extracted,
                                "total": len(extracted),
                                "fallback_used": True,
                                "fallback_reason": "三源 forecast 全空,从研报正文抽取评级/目标价作为简化 forecast",
                                "degraded": True,
                                "quality_flags": ["synthesized_from_research_report"],
                                "source": fn_name,
                            }),
                            canonical_tool="get_profit_forecast",
                            canonical_args=canonical_args,
                            alias_hits=alias_hits,
                        )
                except Exception:
                    continue

        return attach_argument_contract_meta(
            _empty_degraded_response(
                {"stockCode": code, "items": [], "total": 0, "message": f"未获取到 {code} 的盈利预测数据"},
                fallback_reason=f"未获取到 {code} 的盈利预测数据",
                source_chain=[
                    "eastmoney.profit_forecast",
                    "tushare.forecast",
                    "akshare.profit_forecast",
                    "akshare.research_report_extracted",  # P3-5.21
                ],
            ),
            canonical_tool="get_profit_forecast",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )
    except Exception as e:
        return attach_argument_contract_meta(
            fail(f"系统错误: {e}"),
            canonical_tool="get_profit_forecast",
            canonical_args={"code": normalize_code(code) if code else ""},
            alias_hits=[],
        )
