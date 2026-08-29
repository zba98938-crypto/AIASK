"""StrategyLifecycleLedger - 统一策略生命周期状态查询入口

P1-1: 实现统一状态查询类,替换所有多表拼接查询。

设计原则:
1. 单一真相来源 - 所有生命周期状态查询统一入口
2. 证据驱动 - 从物理表和证据表派生业务状态
3. 只读查询 - 不修改数据库,只提供查询接口
4. 批量优化 - 支持批量查询减少数据库往返
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class BusinessLifecycleStage(str, Enum):
    """业务生命周期覆盖层状态(诊断派生,非数据库枚举)"""

    GENERATED = "generated"  # 候选已生成,可进入 gate/admission
    ADMITTED_OBSERVE = "admitted_observe"  # 已进入 observe/paper/diagnostic/warmup 轨道
    PAPER_SIGNALLED = "paper_signalled"  # 策略在可执行宇宙中产生信号
    PAPER_ORDERED = "paper_ordered"  # 非零 signal 已转 paper order
    PAPER_FILLED_OPEN = "paper_filled_open"  # order 已成交并形成持仓入口
    FORWARD_WINDOW_MATURED = "forward_window_matured"  # 前向窗口已有样本
    PAPER_EXITED = "paper_exited"  # 持仓发生退出动作
    ROUND_TRIP_CLOSED = "round_trip_closed"  # 完成真实 paper round-trip
    AUDIT_READY = "audit_ready"  # audit 已运行且 gate status 可解释
    PROMOTION_READY = "promotion_ready"  # 可进入正式晋级讨论

    # 失败状态
    REJECTED = "rejected"  # 候选未通过准入
    DIAGNOSTIC = "diagnostic"  # 仅诊断观察
    BLOCKED = "blocked"  # 当前状态无法推进
    PENDING_EVIDENCE = "pending_evidence"  # 证据方向正确但样本未成熟


@dataclass
class StrategyLifecycleSnapshot:
    """策略生命周期快照"""

    strategy_id: str

    # 物理状态
    physical_status: str  # strategies.status
    incubation_stage: str | None  # strategy_incubation_accounts.stage
    incubation_status: str | None  # strategy_incubation_accounts.status
    pipeline_stage: str | None  # strategy_incubation_pipeline_snapshots.pipeline_stage

    # 业务生命周期状态
    business_stage: BusinessLifecycleStage

    # 证据计数
    signal_count: int
    order_count: int
    trade_count: int
    open_position_count: int
    closed_position_count: int
    forward_return_count: int
    audit_snapshot_count: int

    # 关键指标
    execution_audit_gate_status: str | None
    hard_gate_passed: bool

    # blocker 原因(如果有)
    blocker_reason: str | None

    # 查询时间
    snapshot_at: datetime


class StrategyLifecycleLedger:
    """策略生命周期账本 - 统一状态查询入口"""

    def __init__(self, conn):
        """初始化账本

        Args:
            conn: SQLite 数据库连接(来自 get_connection())
        """
        self.conn = conn

    def get_snapshot(self, strategy_id: str) -> StrategyLifecycleSnapshot:
        """获取单个策略的生命周期快照

        Args:
            strategy_id: 策略 ID

        Returns:
            StrategyLifecycleSnapshot: 策略生命周期快照

        Raises:
            ValueError: 策略不存在
        """
        cursor = self.conn.execute(
            """
            SELECT
                s.id,
                s.status,
                ia.stage as incubation_stage,
                ia.status as incubation_status,
                ips.pipeline_stage
            FROM strategies s
            LEFT JOIN strategy_incubation_accounts ia ON ia.strategy_id = s.id AND ia.status = 'active'
            LEFT JOIN strategy_incubation_pipeline_snapshots ips
                ON ips.strategy_id = s.id
                AND ips.created_at = (
                    SELECT MAX(created_at)
                    FROM strategy_incubation_pipeline_snapshots
                    WHERE strategy_id = s.id
                )
            WHERE s.id = ?
            """,
            (strategy_id,)
        )
        row = cursor.fetchone()

        if not row:
            raise ValueError(f"Strategy {strategy_id} not found")

        # 查询证据计数
        evidence = self._query_evidence_counts(strategy_id)

        # 派生业务生命周期状态
        business_stage, blocker = self._derive_business_stage(
            physical_status=row[1],
            incubation_stage=row[2],
            evidence=evidence
        )

        return StrategyLifecycleSnapshot(
            strategy_id=strategy_id,
            physical_status=row[1],
            incubation_stage=row[2],
            incubation_status=row[3],
            pipeline_stage=row[4],
            business_stage=business_stage,
            signal_count=evidence["signal_count"],
            order_count=evidence["order_count"],
            trade_count=evidence["trade_count"],
            open_position_count=evidence["open_position_count"],
            closed_position_count=evidence["closed_position_count"],
            forward_return_count=evidence["forward_return_count"],
            audit_snapshot_count=evidence["audit_snapshot_count"],
            execution_audit_gate_status=evidence["audit_gate_status"],
            hard_gate_passed=evidence["hard_gate_passed"],
            blocker_reason=blocker,
            snapshot_at=datetime.now()
        )

    def _query_evidence_counts(self, strategy_id: str) -> dict[str, Any]:
        """查询策略的证据计数"""
        # 查询信号数
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM strategy_signals WHERE strategy_id = ? AND COALESCE(signal, 0) != 0",
            (strategy_id,)
        )
        signal_count = cursor.fetchone()[0]

        # 查询订单数
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE strategy_id = ?",
            (strategy_id,)
        )
        order_count = cursor.fetchone()[0]

        # 查询成交数
        cursor = self.conn.execute(
            """
            SELECT COUNT(*) FROM paper_trades pt
            JOIN paper_orders po ON po.id = pt.source_order_id
            WHERE po.strategy_id = ?
            """,
            (strategy_id,)
        )
        trade_count = cursor.fetchone()[0]

        # 查询持仓数
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM strategy_trade_positions WHERE strategy_id = ? AND status = 'open'",
            (strategy_id,)
        )
        open_position_count = cursor.fetchone()[0]

        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM strategy_trade_positions WHERE strategy_id = ? AND status = 'closed'",
            (strategy_id,)
        )
        closed_position_count = cursor.fetchone()[0]

        # 查询前向收益数
        cursor = self.conn.execute(
            """
            SELECT COUNT(*) FROM signal_forward_returns sfr
            JOIN strategy_signals ss ON ss.id = sfr.signal_id
            WHERE ss.strategy_id = ?
            """,
            (strategy_id,)
        )
        forward_return_count = cursor.fetchone()[0]

        # 查询审计快照数
        cursor = self.conn.execute(
            "SELECT COUNT(*), MAX(verdict_status) FROM strategy_execution_audit_snapshots WHERE strategy_id = ?",
            (strategy_id,)
        )
        row = cursor.fetchone()
        audit_snapshot_count = row[0]
        audit_gate_status = row[1]

        hard_gate_passed = audit_gate_status == "passed" if audit_gate_status else False

        return {
            "signal_count": signal_count,
            "order_count": order_count,
            "trade_count": trade_count,
            "open_position_count": open_position_count,
            "closed_position_count": closed_position_count,
            "forward_return_count": forward_return_count,
            "audit_snapshot_count": audit_snapshot_count,
            "audit_gate_status": audit_gate_status,
            "hard_gate_passed": hard_gate_passed,
        }

    def _derive_business_stage(
        self,
        physical_status: str,
        incubation_stage: str | None,
        evidence: dict[str, Any]
    ) -> tuple[BusinessLifecycleStage, str | None]:
        """从物理状态和证据派生业务生命周期状态

        Returns:
            (BusinessLifecycleStage, blocker_reason)
        """
        # 规则 1: rejected
        if physical_status == "rejected":
            return BusinessLifecycleStage.REJECTED, "gate_rejection"

        # 规则 2: diagnostic
        if incubation_stage == "diagnostic":
            return BusinessLifecycleStage.DIAGNOSTIC, "diagnostic_only"

        # 规则 3: admitted_observe
        if physical_status in ("submitted", "incubating", "listed") and incubation_stage:
            # 检查是否有信号
            if evidence["signal_count"] == 0:
                return BusinessLifecycleStage.ADMITTED_OBSERVE, "no_signal_yet"

            # 有信号,进入下一阶段
            if evidence["order_count"] == 0:
                return BusinessLifecycleStage.PAPER_SIGNALLED, "signal_to_order_blocked"

            if evidence["trade_count"] == 0:
                return BusinessLifecycleStage.PAPER_ORDERED, "order_to_trade_blocked"

            if evidence["open_position_count"] == 0 and evidence["closed_position_count"] == 0:
                return BusinessLifecycleStage.PAPER_FILLED_OPEN, "no_position_yet"

            if evidence["forward_return_count"] == 0:
                return BusinessLifecycleStage.PAPER_FILLED_OPEN, "forward_window_pending"

            if evidence["closed_position_count"] == 0:
                return BusinessLifecycleStage.FORWARD_WINDOW_MATURED, "no_exit_yet"

            if evidence["audit_snapshot_count"] == 0:
                return BusinessLifecycleStage.ROUND_TRIP_CLOSED, "audit_not_run"

            if evidence["hard_gate_passed"]:
                return BusinessLifecycleStage.PROMOTION_READY, None
            else:
                audit_status = evidence["audit_gate_status"]
                if audit_status in ("missing", "bootstrap_pending"):
                    return BusinessLifecycleStage.AUDIT_READY, f"audit_{audit_status}"
                elif audit_status == "insufficient_samples":
                    return BusinessLifecycleStage.PENDING_EVIDENCE, "sample_debt"
                else:
                    return BusinessLifecycleStage.AUDIT_READY, f"audit_{audit_status}"

        # 默认: blocked
        return BusinessLifecycleStage.BLOCKED, f"unknown_state_{physical_status}"

    def batch_get_snapshots(self, strategy_ids: list[str]) -> dict[str, StrategyLifecycleSnapshot]:
        """批量获取策略生命周期快照

        Args:
            strategy_ids: 策略 ID 列表

        Returns:
            字典: {strategy_id: StrategyLifecycleSnapshot}
        """
        result = {}
        for strategy_id in strategy_ids:
            try:
                result[strategy_id] = self.get_snapshot(strategy_id)
            except ValueError:
                logger.warning(f"Strategy {strategy_id} not found in batch query")
                continue
        return result
