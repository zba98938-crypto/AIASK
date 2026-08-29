"""因子表现反馈写入器 — 对齐 incubation_factory/feedback_writer.py 模式。

将因子在实际策略中的表现写入数据库，供衰减监控和 Meta-Learner 读取。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class FactorPerformanceFeedbackWriter:
    """因子表现反馈写入器。

    通过 strategy_domain_events 表传递反馈（对齐现有模式）。
    """

    async def write_factor_performance(
        self,
        db: Any,
        *,
        factor_id: str,
        strategy_id: str,
        realized_ic: float,
        realized_turnover: float = 0.0,
        realized_cost: float = 0.0,
        period: str = "",
    ) -> dict[str, Any]:
        """报告因子在实际策略中的表现。"""
        if not hasattr(db, "save_strategy_domain_event"):
            return {"written": False, "reason": "db_not_supported"}

        try:
            await db.save_strategy_domain_event({
                "strategy_id": strategy_id,
                "aggregate_type": "factor_mining_factory",
                "aggregate_id": factor_id,
                "event_type": "factor.performance_reported",
                "source": "factor_mining_factory",
                "severity": "info",
                "payload": {
                    "factor_id": factor_id,
                    "strategy_id": strategy_id,
                    "realized_ic": realized_ic,
                    "realized_turnover": realized_turnover,
                    "realized_cost": realized_cost,
                    "period": period,
                    "reported_at": datetime.now(timezone.utc).isoformat(),
                },
            })
            return {"written": True, "factor_id": factor_id}
        except Exception as exc:
            logger.debug("FactorPerformanceFeedbackWriter: write failed: %s", exc)
            return {"written": False, "error": str(exc)}

    async def write_decay_alert(
        self,
        db: Any,
        *,
        factor_id: str,
        factor_name: str = "",
        decay_rate: float,
        estimated_half_life: float | None = None,
        severity: str = "warning",
    ) -> dict[str, Any]:
        """写入衰减警报。"""
        if not hasattr(db, "save_strategy_domain_event"):
            return {"written": False, "reason": "db_not_supported"}

        try:
            await db.save_strategy_domain_event({
                "strategy_id": None,
                "aggregate_type": "factor_mining_factory",
                "aggregate_id": factor_id,
                "event_type": "factor.decay_alert",
                "source": "factor_mining_factory",
                "severity": severity,
                "payload": {
                    "factor_id": factor_id,
                    "factor_name": factor_name,
                    "decay_rate": decay_rate,
                    "estimated_half_life": estimated_half_life,
                    "alert_at": datetime.now(timezone.utc).isoformat(),
                },
            })
            return {"written": True, "factor_id": factor_id}
        except Exception as exc:
            logger.debug("FactorPerformanceFeedbackWriter: decay alert failed: %s", exc)
            return {"written": False, "error": str(exc)}

    async def write_mining_cycle_report(
        self,
        db: Any,
        *,
        run_id: str,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """写入挖掘周期报告。"""
        if not hasattr(db, "save_strategy_domain_event"):
            return {"written": False, "reason": "db_not_supported"}

        try:
            await db.save_strategy_domain_event({
                "strategy_id": None,
                "aggregate_type": "factor_mining_factory",
                "aggregate_id": run_id,
                "event_type": "factor.mining_cycle_completed",
                "source": "factor_mining_factory",
                "severity": "info",
                "payload": {
                    "run_id": run_id,
                    "raw_candidate_count": report.get("raw_candidate_count", 0),
                    "evolved_count": report.get("evolved_count", 0),
                    "validated_count": report.get("validated_count", 0),
                    "admitted_count": report.get("admitted_count", 0),
                    "pool_size": report.get("pool_size", 0),
                    "engines_used": report.get("engines_used", []),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            })
            return {"written": True, "run_id": run_id}
        except Exception as exc:
            logger.debug("FactorPerformanceFeedbackWriter: cycle report failed: %s", exc)
            return {"written": False, "error": str(exc)}
