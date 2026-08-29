"""
SQLite 适配器 — 财务数据 Mixin

提供 get_financials 方法，兼容 stock_code / code 两种列名。
"""

from typing import List, Dict, Any


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    return text[:10] if text else None


class FinancialsMixin:
    """财务数据查询"""

    async def _financials_columns(self, conn) -> set[str]:
        try:
            rows = await conn.fetch(
                "SELECT name AS column_name FROM pragma_table_info('financials')"
            )
            return {r["column_name"] for r in rows} if rows else set()
        except Exception:
            return set()

    async def _financials_code_column(self, conn) -> str:
        """兼容历史库：financials 可能用 stock_code 或 code 作为代码列"""
        try:
            cols = await self._financials_columns(conn)
            if "stock_code" in cols:
                return "stock_code"
            if "code" in cols:
                return "code"
        except Exception:
            pass
        return "stock_code"

    async def get_financials(
        self,
        code: str,
        limit: int = 4
    ) -> List[Dict[str, Any]]:
        """查询财务数据"""
        async with self.acquire() as conn:
            cols = await self._financials_columns(conn)
            code_col = await self._financials_code_column(conn)
            select_columns = [
                f"{code_col}",
                "report_date",
                "revenue" if "revenue" in cols else "NULL AS revenue",
                "net_profit" if "net_profit" in cols else "NULL AS net_profit",
                "gross_margin" if "gross_margin" in cols else "NULL AS gross_margin",
                "net_margin" if "net_margin" in cols else "NULL AS net_margin",
                "debt_ratio" if "debt_ratio" in cols else "NULL AS debt_ratio",
                "current_ratio" if "current_ratio" in cols else "NULL AS current_ratio",
                "eps" if "eps" in cols else "NULL AS eps",
                "roe" if "roe" in cols else "NULL AS roe",
                "bvps" if "bvps" in cols else "NULL AS bvps",
                "roa" if "roa" in cols else "NULL AS roa",
                "revenue_growth" if "revenue_growth" in cols else "NULL AS revenue_growth",
                "profit_growth" if "profit_growth" in cols else "NULL AS profit_growth",
            ]
            rows = await conn.fetch(
                f"""
                SELECT
                    {", ".join(select_columns)}
                FROM financials
                WHERE {code_col} = $1
                ORDER BY report_date DESC
                LIMIT $2
                """,
                code, limit
            )

            return [
                {
                    'code': row[code_col],
                    'report_date': _date_text(row['report_date']),
                    'revenue': float(row['revenue']) if row['revenue'] is not None else None,
                    'net_profit': float(row['net_profit']) if row['net_profit'] is not None else None,
                    'gross_margin': float(row['gross_margin']) if row['gross_margin'] is not None else None,
                    'net_margin': float(row['net_margin']) if row['net_margin'] is not None else None,
                    'current_ratio': float(row['current_ratio']) if row['current_ratio'] is not None else None,
                    'eps': float(row['eps']) if row['eps'] is not None else None,
                    'roe': float(row['roe']) if row['roe'] is not None else None,
                    'bvps': float(row['bvps']) if row['bvps'] is not None else None,
                    'roa': float(row['roa']) if row['roa'] is not None else None,
                    'debt_ratio': float(row['debt_ratio']) if row['debt_ratio'] is not None else None,
                    'revenue_growth': float(row['revenue_growth']) if row['revenue_growth'] is not None else None,
                    'profit_growth': float(row['profit_growth']) if row['profit_growth'] is not None else None,
                }
                for row in rows
            ]
