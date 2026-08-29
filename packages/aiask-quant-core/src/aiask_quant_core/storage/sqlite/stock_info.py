"""
SQLite 适配器 — 股票信息 Mixin

提供 get_stock_info / search_stocks / list_stock_universe 方法。
"""

import logging
from datetime import date, datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def _normalize_list_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    # SQLite returns date columns as strings (e.g. '2010-08-19') because
    # the underlying type affinity is TEXT. PG returns datetime.date. Be
    # robust to both shapes — calling .strftime on a plain string raises
    # AttributeError, which historically was swallowed by the bare
    # `except Exception: pass` and produced silent dataset corruption
    # (see docs/strategy-factory/策略工厂跑偏修复方案.md). Now we type-
    # check first and only call strftime on real date/datetime objects.
    if isinstance(value, (date, datetime)):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception as exc:
            logger.warning(
                "stock_info._normalize_list_date: strftime failed for %r: %s",
                value, exc, exc_info=True,
            )
    text = str(value).strip()
    return text or None


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class StockInfoMixin:
    """股票基本信息查询"""

    async def _stocks_market_cap_multiplier(self, conn) -> float:
        cached = getattr(self, "_stocks_market_cap_multiplier_cache", None)
        if cached is not None:
            return float(cached)
        try:
            row = await conn.fetchrow("SELECT MAX(market_cap) AS max_market_cap FROM stocks")
            max_market_cap = float((dict(row or {})).get("max_market_cap") or 0.0)
        except Exception as exc:
            logger.warning(
                "stock_info._stocks_market_cap_multiplier: failed to read max market_cap: %s",
                exc, exc_info=True,
            )
            max_market_cap = 0.0
        # Historical stock snapshots may persist market cap in 万元; normalize to 元 on read.
        multiplier = 10_000.0 if 0 < max_market_cap < 10_000_000_000 else 1.0
        setattr(self, "_stocks_market_cap_multiplier_cache", multiplier)
        return multiplier

    @staticmethod
    def _normalize_market_cap(value: Any, multiplier: float) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value) * float(multiplier or 1.0)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _market_cap_filter_to_storage(value: Optional[float], multiplier: float) -> Optional[float]:
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric <= 0:
            return numeric
        scale = float(multiplier or 1.0)
        return numeric / scale if scale > 0 else numeric

    async def _stocks_columns(self, conn) -> set[str]:
        try:
            rows = await conn.fetch(
                "SELECT name AS column_name FROM pragma_table_info('stocks')"
            )
            return {str(r['column_name']) for r in rows} if rows else set()
        except Exception:
            return set()

    async def _stocks_code_column(self, conn) -> str:
        cols = await self._stocks_columns(conn)
        if 'stock_code' in cols:
            return 'stock_code'
        if 'code' in cols:
            return 'code'
        return 'stock_code'

    async def _stocks_market_column(self, conn) -> Optional[str]:
        cols = await self._stocks_columns(conn)
        if 'market' in cols:
            return 'market'
        return None

    async def _stocks_sector_column(self, conn) -> Optional[str]:
        cols = await self._stocks_columns(conn)
        if 'sector' in cols:
            return 'sector'
        if 'industry' in cols:
            return 'industry'
        return None

    async def get_stock_info(self, code: str) -> Optional[Dict[str, Any]]:
        """查询股票基本信息"""
        async with self.acquire() as conn:
            code_col = await self._stocks_code_column(conn)
            market_col = await self._stocks_market_column(conn)
            sector_col = await self._stocks_sector_column(conn)
            market_cap_multiplier = await self._stocks_market_cap_multiplier(conn)
            select_fields = [
                f"{code_col} AS code",
                'stock_name',
                'industry',
                'market_cap',
                'pe_ratio',
                'pb_ratio',
                'list_date',
            ]
            if market_col:
                select_fields.append(f"{market_col} AS market")
            if sector_col and sector_col != 'industry':
                select_fields.append(f"{sector_col} AS sector")
            row = await conn.fetchrow(
                f"""
                SELECT {', '.join(select_fields)}
                FROM stocks
                WHERE {code_col} = $1
                """,
                code,
            )

            if not row:
                return None

            payload = dict(row)
            return {
                'code': payload.get('code'),
                'name': payload.get('stock_name'),
                'industry': payload.get('industry'),
                'sector': payload.get('sector') or payload.get('industry'),
                'market': payload.get('market'),
                'market_cap': self._normalize_market_cap(payload.get('market_cap'), market_cap_multiplier),
                'pe_ratio': _safe_float(payload.get('pe_ratio')),
                'pb_ratio': _safe_float(payload.get('pb_ratio')),
                'list_date': _normalize_list_date(payload.get('list_date')),
            }

    async def search_stocks(self, keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
        """搜索股票（支持代码和名称）"""
        async with self.acquire() as conn:
            code_col = await self._stocks_code_column(conn)
            sector_col = await self._stocks_sector_column(conn)
            market_cap_multiplier = await self._stocks_market_cap_multiplier(conn)
            sector_clause = f" OR ({sector_col} IS NOT NULL AND {sector_col} LIKE $2)" if sector_col else ''
            rows = await conn.fetch(
                f"""
                SELECT {code_col} AS code, stock_name, industry, market_cap
                FROM stocks
                WHERE {code_col} LIKE $1 OR stock_name LIKE $2{sector_clause}
                ORDER BY market_cap DESC NULLS LAST, {code_col}
                LIMIT $3
                """,
                f'%{keyword}%', f'%{keyword}%', limit,
            )

            return [
                {
                    'code': payload.get('code'),
                    'name': payload.get('stock_name'),
                    'industry': payload.get('industry'),
                    'market_cap': self._normalize_market_cap(payload.get('market_cap'), market_cap_multiplier),
                }
                for payload in (dict(row) for row in rows)
            ]

    async def list_stocks_by_codes(self, codes: List[str]) -> List[Dict[str, Any]]:
        """按代码列表批量拉股票元数据，与 list_stock_universe 输出结构一致。

        用于 LLM 研究上下文在 task.target_symbols 命中前 N 大市值之外时，把
        指定股票补进 universe_rows，避免 _build_blocked_research_context 把整条
        LLM 路径 short-circuit 掉。
        """
            
        normalized = [str(item).strip() for item in (codes or []) if str(item).strip()]
        if not normalized:
            return []
        async with self.acquire() as conn:
            code_col = await self._stocks_code_column(conn)
            market_col = await self._stocks_market_column(conn)
            sector_col = await self._stocks_sector_column(conn)
            market_cap_multiplier = await self._stocks_market_cap_multiplier(conn)
            select_fields = [
                f"{code_col} AS code",
                'stock_name',
                'industry',
                'market_cap',
                'pe_ratio',
                'pb_ratio',
                'list_date',
            ]
            if market_col:
                select_fields.append(f"{market_col} AS market")
            if sector_col and sector_col != 'industry':
                select_fields.append(f"{sector_col} AS sector")
            placeholders = ", ".join(f"${i + 1}" for i in range(len(normalized)))
            rows = await conn.fetch(
                f"""
                SELECT {', '.join(select_fields)}
                FROM stocks
                WHERE {code_col} IN ({placeholders})
                """,
                *normalized,
            )
            return [
                {
                    'code': payload.get('code'),
                    'name': payload.get('stock_name'),
                    'industry': payload.get('industry'),
                    'sector': payload.get('sector') or payload.get('industry'),
                    'market': payload.get('market'),
                    'market_cap': self._normalize_market_cap(payload.get('market_cap'), market_cap_multiplier),
                    'pe_ratio': _safe_float(payload.get('pe_ratio')),
                    'pb_ratio': _safe_float(payload.get('pb_ratio')),
                    'list_date': _normalize_list_date(payload.get('list_date')),
                }
                for payload in (dict(row) for row in rows)
            ]

    async def list_stock_universe(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        min_market_cap: Optional[float] = None,
        industry: Optional[str] = None,
        market: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出股票池，用于全市场研究/筛选。"""
        async with self.acquire() as conn:
            code_col = await self._stocks_code_column(conn)
            market_col = await self._stocks_market_column(conn)
            sector_col = await self._stocks_sector_column(conn)
            market_cap_multiplier = await self._stocks_market_cap_multiplier(conn)
            select_fields = [
                f"{code_col} AS code",
                'stock_name',
                'industry',
                'market_cap',
                'pe_ratio',
                'pb_ratio',
                'list_date',
            ]
            if market_col:
                select_fields.append(f"{market_col} AS market")
            if sector_col and sector_col != 'industry':
                select_fields.append(f"{sector_col} AS sector")
            conditions = []
            params: list[Any] = []
            # PR-U5: 默认排除北交所（920xxx）、B 股（200xxx、900xxx）、老三板（8xxxxx）
            conditions.append(
                f"{code_col} NOT LIKE '920%' AND {code_col} NOT LIKE '200%'"
                f" AND {code_col} NOT LIKE '900%' AND {code_col} NOT LIKE '8%'"
            )
            if min_market_cap is not None:
                params.append(self._market_cap_filter_to_storage(min_market_cap, market_cap_multiplier))
                conditions.append(f"market_cap >= ${len(params)}")
            if industry:
                params.append(f"%{industry}%")
                conditions.append(f"industry ILIKE ${len(params)}")
            if market and market_col:
                params.append(market)
                conditions.append(f"{market_col} = ${len(params)}")
            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ''
            params.append(max(1, min(int(limit or 200), 10000)))
            params.append(max(0, int(offset or 0)))
            rows = await conn.fetch(
                f"""
                SELECT {', '.join(select_fields)}
                FROM stocks
                {where_clause}
                ORDER BY market_cap DESC NULLS LAST, {code_col}
                LIMIT ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
            return [
                {
                    'code': payload.get('code'),
                    'name': payload.get('stock_name'),
                    'industry': payload.get('industry'),
                    'sector': payload.get('sector') or payload.get('industry'),
                    'market': payload.get('market'),
                    'market_cap': self._normalize_market_cap(payload.get('market_cap'), market_cap_multiplier),
                    'pe_ratio': _safe_float(payload.get('pe_ratio')),
                    'pb_ratio': _safe_float(payload.get('pb_ratio')),
                    'list_date': _normalize_list_date(payload.get('list_date')),
                }
                for payload in (dict(row) for row in rows)
            ]

    async def count_stock_universe(
        self,
        *,
        min_market_cap: Optional[float] = None,
        industry: Optional[str] = None,
        market: Optional[str] = None,
    ) -> int:
        """统计股票池数量。"""
        async with self.acquire() as conn:
            market_col = await self._stocks_market_column(conn)
            market_cap_multiplier = await self._stocks_market_cap_multiplier(conn)
            conditions = []
            params: list[Any] = []
            if min_market_cap is not None:
                params.append(self._market_cap_filter_to_storage(min_market_cap, market_cap_multiplier))
                conditions.append(f"market_cap >= ${len(params)}")
            if industry:
                params.append(f"%{industry}%")
                conditions.append(f"industry ILIKE ${len(params)}")
            if market and market_col:
                params.append(market)
                conditions.append(f"{market_col} = ${len(params)}")
            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ''
            row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total FROM stocks {where_clause}",
                *params,
            )
            payload = dict(row or {})
            return int(payload.get('total') or 0)
