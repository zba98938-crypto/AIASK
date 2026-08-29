"""
SQLite 适配器 — 实时行情与统计 Mixin

提供 save_quote / get_stats 方法。
"""

from typing import Dict, Any, Optional
from datetime import datetime


class QuotesMixin:
    """实时行情保存与数据库统计"""

    async def _stock_quote_columns(self, conn) -> set[str]:
        cached = getattr(self, "_stock_quotes_columns_cache", None)
        if isinstance(cached, set) and cached:
            return cached
        rows = await conn.fetch(
            "SELECT name AS column_name FROM pragma_table_info('stock_quotes')"
        )
        columns = {str(row["column_name"]) for row in rows or []}
        if columns:
            self._stock_quotes_columns_cache = columns
        return columns

    async def save_quote(self, quote: Dict[str, Any]) -> None:
        """保存实时行情（统一字段映射）

        字段映射规则（对齐Node版本）：
        - prev_close（标准） ← pre_close（兼容）
        - change_amt（标准） ← change（兼容）
        - mkt_cap（标准） ← market_cap（兼容）
        """
        async with self.acquire() as conn:
            normalized_quote = {
                'code': quote.get('code'),
                'name': quote.get('name'),
                'price': quote.get('price'),
                'change_amt': quote.get('change_amt') or quote.get('change'),
                'change_pct': quote.get('change_pct'),
                'open': quote.get('open'),
                'high': quote.get('high'),
                'low': quote.get('low'),
                'prev_close': quote.get('prev_close') or quote.get('pre_close'),
                'volume': quote.get('volume'),
                'amount': quote.get('amount'),
                'pe': quote.get('pe'),
                'pb': quote.get('pb'),
                'mkt_cap': quote.get('mkt_cap') or quote.get('market_cap'),
            }

            available_columns = await self._stock_quote_columns(conn)
            insert_columns = ["time"] + [name for name in normalized_quote.keys() if name in available_columns]
            update_columns = [name for name in insert_columns if name not in {"time", "code"}]
            placeholders = [f"${idx}" for idx in range(1, len(insert_columns) + 1)]
            update_clause = ", ".join(f"{name} = EXCLUDED.{name}" for name in update_columns) or "code = EXCLUDED.code"
            values = [datetime.now()] + [normalized_quote[name] for name in insert_columns[1:]]

            await conn.execute(
                f"""
                INSERT INTO stock_quotes ({", ".join(insert_columns)})
                VALUES ({", ".join(placeholders)})
                ON CONFLICT (time, code) DO UPDATE SET
                    {update_clause}
                """,
                *values,
            )

    async def get_latest_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """Return the newest quote snapshot for ``code`` from SQLite only."""
        normalized = str(code or "").strip()
        if not normalized:
            return None
        bare = normalized.split(".", 1)[0] if "." in normalized else normalized
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    time, code, name, price, change_pct, change_amt,
                    open, high, low, prev_close, volume, amount,
                    pe, pb, mkt_cap, updated_at
                FROM stock_quotes
                WHERE code IN ($1, $2)
                ORDER BY time DESC
                LIMIT 1
                """,
                normalized,
                bare,
            )
        if not row:
            return None
        payload = dict(row)
        payload.setdefault("source", "stock_quotes")
        return payload

    async def get_stats(self) -> Dict[str, int]:
        """获取数据库统计信息"""
        async with self.acquire() as conn:
            stock_count = await conn.fetchval("SELECT COUNT(*) FROM stocks")
            kline_count = await conn.fetchval("SELECT COUNT(*) FROM kline_1d")
            financial_count = await conn.fetchval("SELECT COUNT(*) FROM financials")
            quote_count = await conn.fetchval("SELECT COUNT(*) FROM stock_quotes")

            return {
                'stock_count': stock_count or 0,
                'kline_count': kline_count or 0,
                'financial_count': financial_count or 0,
                'quote_count': quote_count or 0,
            }
