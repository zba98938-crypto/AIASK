"""研究报告工具（DB版本）— 从 SQLite research_reports 表搜索、分析和汇总。

与 news/research.py（Tushare 外部 API 版本）互补。
"""

import time
from datetime import date, timedelta
from .manager_protocol import ok_with_meta
from ..utils import fail, resolve_existing_security_code_async, validate_int_range


def register(mcp):
    @mcp.tool()
    async def search_research_db(keyword: str = None, stock_code: str = None, days: int = 30):
        """从数据库搜索研究报告

        Args:
            keyword: 搜索关键词（标题或摘要）
            stock_code: 股票代码
            days: 查询最近多少天（默认30）
        """
        started_at = time.perf_counter()
        try:
            from ..storage import get_db
            db = get_db()
            if stock_code:
                stock_code, _, error = await resolve_existing_security_code_async(stock_code=stock_code)
                if error:
                    return fail(error)
            since = date.today() - timedelta(days=days)
            conditions = ["publish_date >= $1"]
            params: list = [since]
            idx = 2

            if stock_code:
                conditions.append(f"code = ${idx}")
                params.append(stock_code.strip())
                idx += 1
            if keyword:
                conditions.append(f"(title ILIKE ${idx} OR summary ILIKE ${idx})")
                params.append(f"%{keyword.strip()}%")
                idx += 1

            where = " AND ".join(conditions)
            query = f"SELECT * FROM research_reports WHERE {where} ORDER BY publish_date DESC LIMIT 50"
            async with db.acquire() as conn:
                rows = await conn.fetch(query, *params)

            reports = [dict(r) for r in rows] if rows else []
            return ok_with_meta(
                {'reports': reports, 'count': len(reports), 'keyword': keyword, 'stock_code': stock_code},
                tool_name="search_research_db",
                action="search",
                started_at=started_at,
                source_chain=["research.db"],
                extra_meta={
                    "degraded": len(reports) == 0,
                    "quality": {
                        "status": "degraded" if len(reports) == 0 else "available",
                        "record_count": len(reports),
                        "fallback_reason": "empty_research_result" if len(reports) == 0 else None,
                    },
                },
            )
        except Exception as e:
            return ok_with_meta(
                {'reports': [], 'count': 0, 'keyword': keyword, 'stock_code': stock_code, 'note': f'DB查询失败: {e}'},
                tool_name="search_research_db",
                action="search",
                started_at=started_at,
                source_chain=["research.db"],
                extra_meta={"degraded": True, "quality": {"status": "degraded", "reason": "db_query_failed"}},
            )

    @mcp.tool()
    async def analyze_research_report(code: str):
        """分析指定股票的最新研究报告

        Args:
            code: 股票代码
        """
        started_at = time.perf_counter()
        try:
            code, _, error = await resolve_existing_security_code_async(code=code)
            if error:
                return fail(error)
            from ..storage import get_db
            db = get_db()
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM research_reports WHERE code = $1 ORDER BY publish_date DESC LIMIT 10",
                    code.strip(),
                )

            reports = [dict(r) for r in rows] if rows else []
            ratings = [r['rating'] for r in reports if r.get('rating')]
            target_prices = [float(r['target_price']) for r in reports if r.get('target_price')]
            institutions = list({r['institution'] for r in reports if r.get('institution')})

            analysis = {
                'code': code,
                'report_count': len(reports),
                'latest_reports': reports[:5],
                'rating_distribution': {r: ratings.count(r) for r in set(ratings)} if ratings else {},
                'avg_target_price': round(sum(target_prices) / len(target_prices), 2) if target_prices else None,
                'institutions': institutions[:10],
                'signals': [],
            }
            return ok_with_meta(
                analysis,
                tool_name="analyze_research_report",
                action="analyze",
                started_at=started_at,
                source_chain=["research.db"],
                extra_meta={
                    "degraded": len(reports) == 0,
                    "quality": {
                        "status": "degraded" if len(reports) == 0 else "available",
                        "report_count": len(reports),
                        "fallback_reason": "empty_research_result" if len(reports) == 0 else None,
                    },
                },
            )
        except Exception as e:
            return ok_with_meta(
                {
                    'code': code,
                    'report_count': 0,
                    'latest_reports': [],
                    'rating_distribution': {},
                    'avg_target_price': None,
                    'institutions': [],
                    'signals': [],
                    'note': f'DB查询失败: {e}',
                },
                tool_name="analyze_research_report",
                action="analyze",
                started_at=started_at,
                source_chain=["research.db"],
                extra_meta={"degraded": True, "quality": {"status": "degraded", "reason": "db_query_failed"}},
            )

    @mcp.tool()
    async def get_research_summary(code: str, limit: int = 10):
        """获取指定股票的研报摘要列表

        Args:
            code: 股票代码
            limit: 返回数量上限（默认10）
        """
        started_at = time.perf_counter()
        try:
            code, _, error = await resolve_existing_security_code_async(code=code)
            if error:
                return fail(error)
            limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1, maximum=50)
            if limit_error:
                return fail(limit_error)
            from ..storage import get_db
            db = get_db()
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT title, rating, target_price, institution, analyst, publish_date, summary "
                    "FROM research_reports WHERE code = $1 ORDER BY publish_date DESC LIMIT $2",
                    code.strip(), limit,
                )

            summaries = [dict(r) for r in rows] if rows else []
            return ok_with_meta(
                {'code': code, 'summaries': summaries, 'count': len(summaries)},
                tool_name="get_research_summary",
                action="summary",
                started_at=started_at,
                source_chain=["research.db"],
                extra_meta={
                    "degraded": len(summaries) == 0,
                    "quality": {
                        "status": "degraded" if len(summaries) == 0 else "available",
                        "record_count": len(summaries),
                        "fallback_reason": "empty_research_result" if len(summaries) == 0 else None,
                    },
                },
            )
        except Exception as e:
            return ok_with_meta(
                {'code': code, 'summaries': [], 'count': 0, 'note': f'DB查询失败: {e}'},
                tool_name="get_research_summary",
                action="summary",
                started_at=started_at,
                source_chain=["research.db"],
                extra_meta={"degraded": True, "quality": {"status": "degraded", "reason": "db_query_failed"}},
            )
