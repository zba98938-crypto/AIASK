"""Strategy Execution Universe runtime implementation.

SignalTracker and Incubation Factory still use this compatibility runtime
inside akshare-mcp, but the canonical contract now lives in strategy-factory.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional

from strategy_factory.api.contracts import ExecutionUniverseContract


class ExecutionMode(str, Enum):
    OBSERVE = "observe"
    PAPER = "paper"
    LIVE = "live"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class ExecutableStrategy:
    strategy_id: str
    account_id: str | None
    execution_mode: ExecutionMode
    incubation_stage: str
    incubation_status: str
    physical_status: str
    runtime_control_enabled: bool
    runtime_control_reason: str | None
    strategy_name: str | None
    created_at: str | date | None
    updated_at: str | date | None


@dataclass(frozen=True)
class ExecutionUniverseSnapshot:
    as_of: date
    total_count: int
    observe_count: int
    paper_count: int
    live_count: int
    diagnostic_count: int
    blocked_count: int
    strategies: list[ExecutableStrategy] = field(default_factory=list)
    signal_tracker_aligned: bool = True
    incubation_aligned: bool = True
    consistency_check_passed: bool = True


class StrategyExecutionUniverse(ExecutionUniverseContract):
    """Legacy runtime implementation kept for sidecar compatibility."""

    def __init__(self, db_connection: sqlite3.Connection):
        self.conn = db_connection

    async def list_executable_strategies(
        self,
        as_of: date,
        modes: Optional[list[ExecutionMode]] = None,
        include_blocked: bool = False,
    ) -> list[ExecutableStrategy]:
        if modes is None:
            modes = [ExecutionMode.OBSERVE, ExecutionMode.PAPER, ExecutionMode.DIAGNOSTIC]

        mode_values = [m.value for m in modes]
        placeholders = ",".join("?" * len(mode_values))
        query = f"""
            SELECT
                s.id as strategy_id,
                sia.account_id,
                sia.execution_mode,
                sia.stage as incubation_stage,
                sia.status as incubation_status,
                s.status as physical_status,
                sia.runtime_control_enabled,
                sia.runtime_control_reason,
                s.name as strategy_name,
                s.created_at,
                s.updated_at
            FROM strategies s
            INNER JOIN strategy_incubation_accounts sia
                ON s.id = sia.strategy_id
            WHERE s.status IN ('submitted', 'incubating', 'listed')
                AND sia.status = 'active'
                AND sia.execution_mode IN ({placeholders})
        """

        params = mode_values
        if not include_blocked:
            query += " AND (sia.runtime_control_enabled = 0 OR sia.runtime_control_enabled IS NULL)"
        query += " ORDER BY s.id"

        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()
        return [
            ExecutableStrategy(
                strategy_id=row[0],
                account_id=row[1],
                execution_mode=ExecutionMode(row[2]),
                incubation_stage=row[3] or "unknown",
                incubation_status=row[4] or "unknown",
                physical_status=row[5] or "unknown",
                runtime_control_enabled=bool(row[6]),
                runtime_control_reason=row[7],
                strategy_name=row[8],
                created_at=row[9] or "",
                updated_at=row[10] or "",
            )
            for row in rows
        ]

    async def get_execution_universe_snapshot(
        self,
        as_of: date,
        modes: Optional[list[ExecutionMode]] = None,
    ) -> ExecutionUniverseSnapshot:
        all_strategies = await self.list_executable_strategies(
            as_of=as_of,
            modes=modes,
            include_blocked=True,
        )

        observe_count = sum(1 for s in all_strategies if s.execution_mode == ExecutionMode.OBSERVE)
        paper_count = sum(1 for s in all_strategies if s.execution_mode == ExecutionMode.PAPER)
        live_count = sum(1 for s in all_strategies if s.execution_mode == ExecutionMode.LIVE)
        diagnostic_count = sum(1 for s in all_strategies if s.execution_mode == ExecutionMode.DIAGNOSTIC)
        blocked_count = sum(1 for s in all_strategies if s.runtime_control_enabled)

        return ExecutionUniverseSnapshot(
            as_of=as_of,
            total_count=len(all_strategies),
            observe_count=observe_count,
            paper_count=paper_count,
            live_count=live_count,
            diagnostic_count=diagnostic_count,
            blocked_count=blocked_count,
            strategies=all_strategies,
            signal_tracker_aligned=True,
            incubation_aligned=True,
            consistency_check_passed=True,
        )

    async def check_strategy_executable(
        self,
        strategy_id: str,
        as_of: date,
    ) -> tuple[bool, Optional[str]]:
        query = """
            SELECT
                s.status,
                sia.account_id,
                sia.status,
                sia.execution_mode,
                sia.runtime_control_enabled,
                sia.runtime_control_reason
            FROM strategies s
            LEFT JOIN strategy_incubation_accounts sia
                ON s.id = sia.strategy_id AND sia.status = 'active'
            WHERE s.id = ?
        """

        cursor = self.conn.execute(query, (strategy_id,))
        row = cursor.fetchone()
        if not row:
            return False, f"strategy {strategy_id} not found"

        physical_status = row[0]
        account_id = row[1]
        account_status = row[2]
        execution_mode = row[3]
        runtime_control_enabled = row[4]
        runtime_control_reason = row[5]

        if physical_status not in ("submitted", "incubating", "listed"):
            return False, f"physical status {physical_status} not executable"
        if not account_id:
            return False, "missing active incubation account"
        if account_status != "active":
            return False, f"account status {account_status} is not active"
        if runtime_control_enabled:
            reason = runtime_control_reason or "unknown reason"
            return False, f"runtime_control blocked: {reason}"
        if execution_mode == "live":
            return False, "live mode requires extra authorization"
        return True, None


__all__ = [
    "ExecutableStrategy",
    "ExecutionMode",
    "ExecutionUniverseSnapshot",
    "StrategyExecutionUniverse",
]
