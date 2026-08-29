"""SQLite 适配器 — 因子持久化 Mixin"""

import logging
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


class FactorStorageMixin:
    """因子值与 IC 历史持久化"""

    @staticmethod
    def _normalize_factor_value_rows(entries: Iterable[Any]) -> List[tuple[str, date, str, float]]:
        rows: List[tuple[str, date, str, float]] = []
        for entry in list(entries or []):
            if isinstance(entry, dict) and isinstance(entry.get("values"), dict):
                stock_code = str(entry.get("stock_code") or "").strip()
                factor_date = entry.get("factor_date")
                values = dict(entry.get("values") or {})
                if not stock_code or factor_date is None:
                    continue
                for factor_name, factor_value in values.items():
                    if factor_value is None:
                        continue
                    rows.append((stock_code, factor_date, str(factor_name), float(factor_value)))
                continue
            if isinstance(entry, (list, tuple)) and len(entry) == 4:
                stock_code = str(entry[0] or "").strip()
                factor_date = entry[1]
                factor_name = str(entry[2] or "").strip()
                factor_value = entry[3]
                if not stock_code or factor_date is None or not factor_name or factor_value is None:
                    continue
                rows.append((stock_code, factor_date, factor_name, float(factor_value)))
        return rows

    async def save_factor_values_batch(self, entries: Iterable[Any], batch_size: int = 1000) -> int:
        rows = self._normalize_factor_value_rows(entries)
        if not rows:
            return 0

        saved = 0
        normalized_batch_size = max(1, int(batch_size or 1000))
        async with self.acquire() as conn:
            for start in range(0, len(rows), normalized_batch_size):
                chunk = rows[start:start + normalized_batch_size]
                await conn.executemany(
                    """
                    INSERT INTO factor_values (stock_code, factor_date, factor_name, factor_value)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (stock_code, factor_date, factor_name) DO UPDATE SET
                        factor_value = EXCLUDED.factor_value,
                        computed_at = CURRENT_TIMESTAMP
                    """,
                    chunk,
                )
                saved += len(chunk)
        return saved

    async def save_factor_values(self, stock_code: str, factor_date: date, values: Dict[str, float]) -> int:
        return await self.save_factor_values_batch(
            [{"stock_code": stock_code, "factor_date": factor_date, "values": values}],
            batch_size=max(len(values or {}), 1),
        )

    async def get_factor_values(self, stock_codes: List[str], factor_name: str, start_date: date = None, end_date: date = None) -> List[dict]:
        async with self.acquire() as conn:
            if start_date and end_date:
                rows = await conn.fetch(
                    """
                    SELECT * FROM factor_values
                    WHERE stock_code IN ($1) AND factor_name = $2 AND factor_date BETWEEN $3 AND $4
                    ORDER BY factor_date, stock_code
                    """,
                    stock_codes, factor_name, start_date, end_date,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM factor_values
                    WHERE stock_code IN ($1) AND factor_name = $2
                    ORDER BY factor_date DESC, stock_code
                    LIMIT 1000
                    """,
                    stock_codes, factor_name,
                )
        return [dict(r) for r in rows]

    async def save_factor_ic(self, factor_name: str, period: str, ic_date: date, ic_value: float, rank_ic: float = None, stock_count: int = None) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO factor_ic_history (factor_name, period, ic_date, ic_value, rank_ic, stock_count)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (factor_name, period, ic_date) DO UPDATE SET
                    ic_value = EXCLUDED.ic_value,
                    rank_ic = EXCLUDED.rank_ic,
                    stock_count = EXCLUDED.stock_count,
                    computed_at = CURRENT_TIMESTAMP
                """,
                factor_name, period, ic_date, ic_value, rank_ic, stock_count,
            )

    async def get_factor_ic_history(self, factor_name: str, period: str = "20", limit: int = 60) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM factor_ic_history
                WHERE factor_name = $1 AND period = $2
                ORDER BY ic_date DESC LIMIT $3
                """,
                factor_name, period, limit,
            )
        return [dict(r) for r in rows]

    async def count_factor_ic_history(self, factor_name: str, period: str | None = None) -> int:
        """统计某因子已持久化的 IC 历史行数(跨轮累计)。

        晋升证据校验应基于累计历史,而非单轮验证窗口新增的行数,
        否则形成"要 60 行才放行、但单轮产不出 60 行"的鸡生蛋死锁。
        period 为 None 时统计该因子全部 period 的累计行数。
        """
        async with self.acquire() as conn:
            if period is None:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS n FROM factor_ic_history WHERE factor_name = $1",
                    factor_name,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS n FROM factor_ic_history WHERE factor_name = $1 AND period = $2",
                    factor_name, period,
                )
        return int(dict(row or {}).get("n") or 0)
