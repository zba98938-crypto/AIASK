"""Canonical execution-universe contract owned by Strategy Factory."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionUniverseQuery:
    """Filters for executable strategy-universe queries."""

    as_of: date | None = None
    include_incubating: bool = True
    include_paper: bool = True
    include_diagnostic: bool = False
    include_listed: bool = False
    limit: int = 500


@dataclass(frozen=True)
class ExecutionUniverseStrategy:
    """Normalized executable-strategy row shared by sidecar runtimes."""

    strategy_id: str
    strategy_name: str | None
    strategy_type: str | None
    status: str
    incubation_stage: str | None
    incubation_status: str | None
    account_id: str | None
    created_at: datetime | None


class ExecutionUniverseContract:
    """Unified query contract for executable strategies across factory runtimes."""

    async def list_executable_strategies(
        self,
        db: Any,
        query: ExecutionUniverseQuery | None = None,
    ) -> list[ExecutionUniverseStrategy]:
        if query is None:
            query = ExecutionUniverseQuery()

        strategies: list[ExecutionUniverseStrategy] = []

        if query.include_incubating:
            strategies.extend(await self._list_incubating_with_accounts(db, query))
        if query.include_paper:
            strategies.extend(await self._list_paper_observation(db, query))
        if query.include_diagnostic:
            strategies.extend(await self._list_diagnostic_observation(db, query))
        if query.include_listed:
            strategies.extend(await self._list_listed_strategies(db, query))

        seen: set[str] = set()
        unique: list[ExecutionUniverseStrategy] = []
        for item in strategies:
            if item.strategy_id in seen:
                continue
            seen.add(item.strategy_id)
            unique.append(item)

        if query.limit > 0 and len(unique) > query.limit:
            unique = unique[: query.limit]

        logger.info(
            "ExecutionUniverseContract: listed %d executable strategies "
            "(incubating=%s, paper=%s, diagnostic=%s, listed=%s, limit=%d)",
            len(unique),
            query.include_incubating,
            query.include_paper,
            query.include_diagnostic,
            query.include_listed,
            query.limit,
        )
        return unique

    async def _list_incubating_with_accounts(
        self,
        db: Any,
        query: ExecutionUniverseQuery,
    ) -> list[ExecutionUniverseStrategy]:
        if not hasattr(db, "execute"):
            return []

        try:
            cursor = await db.execute(
                """
                SELECT
                    s.id, s.name, s.strategy_type, s.status, s.created_at,
                    ia.stage, ia.status as account_status, ia.id as account_id
                FROM strategies s
                INNER JOIN strategy_incubation_accounts ia
                    ON s.id = ia.strategy_id
                WHERE s.status = 'incubating'
                    AND ia.status = 'active'
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (query.limit,),
            )
            rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("ExecutionUniverseContract: incubating query failed: %s", exc)
            return []

        return [
            ExecutionUniverseStrategy(
                strategy_id=str(row[0]),
                strategy_name=row[1],
                strategy_type=row[2],
                status=row[3],
                created_at=self._parse_datetime(row[4]),
                incubation_stage=row[5],
                incubation_status=row[6],
                account_id=str(row[7]) if row[7] else None,
            )
            for row in rows
        ]

    async def _list_paper_observation(
        self,
        db: Any,
        query: ExecutionUniverseQuery,
    ) -> list[ExecutionUniverseStrategy]:
        if not hasattr(db, "execute"):
            return []

        try:
            cursor = await db.execute(
                """
                SELECT
                    s.id, s.name, s.strategy_type, s.status, s.created_at,
                    ia.stage, ia.status as account_status, ia.id as account_id
                FROM strategies s
                INNER JOIN strategy_incubation_accounts ia
                    ON s.id = ia.strategy_id
                WHERE ia.stage = 'warmup'
                    AND ia.status = 'active'
                    AND s.status NOT IN ('rejected', 'deprecated', 'archived')
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (query.limit,),
            )
            rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("ExecutionUniverseContract: paper observation query failed: %s", exc)
            return []

        return [
            ExecutionUniverseStrategy(
                strategy_id=str(row[0]),
                strategy_name=row[1],
                strategy_type=row[2],
                status=row[3],
                created_at=self._parse_datetime(row[4]),
                incubation_stage=row[5],
                incubation_status=row[6],
                account_id=str(row[7]) if row[7] else None,
            )
            for row in rows
        ]

    async def _list_diagnostic_observation(
        self,
        db: Any,
        query: ExecutionUniverseQuery,
    ) -> list[ExecutionUniverseStrategy]:
        if not hasattr(db, "execute"):
            return []

        try:
            cursor = await db.execute(
                """
                SELECT
                    s.id, s.name, s.strategy_type, s.status, s.created_at,
                    ia.stage, ia.status as account_status, ia.id as account_id
                FROM strategies s
                INNER JOIN strategy_incubation_accounts ia
                    ON s.id = ia.strategy_id
                WHERE ia.stage = 'diagnostic'
                    AND ia.status = 'active'
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (query.limit,),
            )
            rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("ExecutionUniverseContract: diagnostic observation query failed: %s", exc)
            return []

        return [
            ExecutionUniverseStrategy(
                strategy_id=str(row[0]),
                strategy_name=row[1],
                strategy_type=row[2],
                status=row[3],
                created_at=self._parse_datetime(row[4]),
                incubation_stage=row[5],
                incubation_status=row[6],
                account_id=str(row[7]) if row[7] else None,
            )
            for row in rows
        ]

    async def _list_listed_strategies(
        self,
        db: Any,
        query: ExecutionUniverseQuery,
    ) -> list[ExecutionUniverseStrategy]:
        if not hasattr(db, "execute"):
            return []

        try:
            cursor = await db.execute(
                """
                SELECT
                    s.id, s.name, s.strategy_type, s.status, s.created_at,
                    ia.stage, ia.status as account_status, ia.id as account_id
                FROM strategies s
                LEFT JOIN strategy_incubation_accounts ia
                    ON s.id = ia.strategy_id AND ia.status = 'active'
                WHERE s.status = 'listed'
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (query.limit,),
            )
            rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("ExecutionUniverseContract: listed strategies query failed: %s", exc)
            return []

        return [
            ExecutionUniverseStrategy(
                strategy_id=str(row[0]),
                strategy_name=row[1],
                strategy_type=row[2],
                status=row[3],
                created_at=self._parse_datetime(row[4]),
                incubation_stage=row[5],
                incubation_status=row[6],
                account_id=str(row[7]) if row[7] else None,
            )
            for row in rows
        ]

    def _parse_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return None
        return None


ExecutableStrategy = ExecutionUniverseStrategy


__all__ = [
    "ExecutableStrategy",
    "ExecutionUniverseContract",
    "ExecutionUniverseQuery",
    "ExecutionUniverseStrategy",
]
