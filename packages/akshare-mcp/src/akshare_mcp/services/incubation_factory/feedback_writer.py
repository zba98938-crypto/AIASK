"""孵化工厂 · 反馈写入模块。

负责将孵化结果写入数据库，供策略工厂的 IncubationBudgeter 读取。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _prediction_budget_feedback_enabled() -> bool:
    return str(
        os.getenv("STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED") or "0"
    ).strip().lower() in {"1", "true", "yes", "on"}


class FeedbackWriter:
    """反馈写入器。

    将孵化工厂的验证结果写入数据库，形成策略工厂的进化反馈：
    - strategy_incubation_metrics: 每日指标（IncubationBudgeter 读取）
    - strategy_domain_events: 领域事件（审计追踪）
    - strategy_closure_snapshots: 闭合快照（晋升/淘汰决策依据）
    """

    async def write(
        self,
        db: Any,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """
        将命中率报告的反馈写入数据库。

        策略工厂通过 IncubationBudgeter._resolve_budget_feedback_root() 读取
        strategy_incubation_metrics 表中的数据，自动调整生产配额。

        Args:
            db: 数据库连接
            report: HitRateReporter 生成的命中率报告

        Returns:
            写入结果摘要
        """
        feedback_actions = dict(report.get("feedback_actions") or {})
        by_family = dict(
            (report.get("hit_rate_dashboard") or {}).get("by_family") or {}
        )

        written_events = 0
        written_controls = 0

        # 1. 写入反馈领域事件（策略工厂可查询）
        await self._write_feedback_event(db, report)
        written_events += 1
        prediction_feedback = dict(feedback_actions.get("prediction_feedback") or {})
        if prediction_feedback:
            await self._write_prediction_feedback_event(db, report, prediction_feedback)
            written_events += 1

        # 2. 对需要冷却/冻结的 family，更新相关策略的 runtime_control
        families_to_cooldown = list(feedback_actions.get("families_to_cooldown") or [])
        families_to_freeze = list(feedback_actions.get("families_to_freeze") or [])

        if families_to_cooldown or families_to_freeze:
            control_result = await self._apply_control_actions(
                db,
                families_to_cooldown=families_to_cooldown,
                families_to_freeze=families_to_freeze,
            )
            written_controls = int(control_result.get("applied") or 0)

        # 3. 写入汇总快照（供 desktop 前端展示）
        await self._write_summary_snapshot(db, report)

        result = {
            "written_events": written_events,
            "written_controls": written_controls,
            "prediction_budget_feedback_enabled": _prediction_budget_feedback_enabled(),
            "prediction_feedback_suggestions": list(prediction_feedback.get("suggestions") or []),
            "feedback_families_boosted": list(
                feedback_actions.get("families_to_boost") or []
            ),
            "feedback_families_cooled": families_to_cooldown,
            "feedback_families_frozen": families_to_freeze,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "FeedbackWriter: wrote feedback (events=%d, controls=%d, boost=%s, cool=%s)",
            written_events,
            written_controls,
            feedback_actions.get("families_to_boost"),
            families_to_cooldown + families_to_freeze,
        )

        return result

    async def _write_feedback_event(
        self, db: Any, report: dict[str, Any]
    ) -> None:
        """写入反馈领域事件。"""
        if not hasattr(db, "save_strategy_domain_event"):
            return
        try:
            await db.save_strategy_domain_event({
                "strategy_id": None,
                "aggregate_type": "incubation_factory",
                "aggregate_id": f"feedback_{report.get('report_date', date.today())}",
                "event_type": "incubation_factory.feedback_written",
                "source": "incubation_factory",
                "severity": "info",
                "payload": {
                    "report_date": report.get("report_date"),
                    "summary": report.get("summary"),
                    "feedback_actions": report.get("feedback_actions"),
                    "overall_hit_rate": (
                        (report.get("hit_rate_dashboard") or {}).get("overall") or {}
                    ).get("hit_rate"),
                    "overall_skill_lcb": (
                        (report.get("hit_rate_dashboard") or {}).get("overall") or {}
                    ).get("avg_skill_lcb"),
                },
            })
        except Exception as exc:
            logger.debug("FeedbackWriter: event write failed: %s", exc)

    async def _write_prediction_feedback_event(
        self,
        db: Any,
        report: dict[str, Any],
        prediction_feedback: dict[str, Any],
    ) -> None:
        """Write prediction feedback diagnostics; budget impact is opt-in."""

        if not hasattr(db, "save_strategy_domain_event"):
            return
        enabled = _prediction_budget_feedback_enabled()
        suggestions = list(prediction_feedback.get("suggestions") or [])
        budget_suggestions: list[dict[str, Any]] = []
        if enabled:
            for item in suggestions:
                if not isinstance(item, dict):
                    continue
                action = str(item.get("action") or "").strip().lower()
                multiplier = 1.0
                if action == "boost":
                    multiplier = 1.08
                elif action in {"cool", "repair_data"}:
                    multiplier = 0.92
                budget_suggestions.append({**item, "budget_multiplier": multiplier})
        try:
            await db.save_strategy_domain_event({
                "strategy_id": None,
                "aggregate_type": "incubation_factory",
                "aggregate_id": f"prediction_feedback_{report.get('report_date', date.today())}",
                "event_type": "incubation_factory.prediction_feedback_written",
                "source": "incubation_factory",
                "severity": "info",
                "payload": {
                    "report_date": report.get("report_date"),
                    "enabled_for_budget_feedback": enabled,
                    "prediction_feedback": prediction_feedback,
                    "budget_suggestions": budget_suggestions,
                    "trade_prediction_dashboard": report.get("trade_prediction_dashboard"),
                },
            })
        except Exception as exc:
            logger.debug("FeedbackWriter: prediction feedback event write failed: %s", exc)

    async def _apply_control_actions(
        self,
        db: Any,
        *,
        families_to_cooldown: list[str],
        families_to_freeze: list[str],
    ) -> dict[str, Any]:
        """对表现差的 family 应用控制动作。"""
        if not hasattr(db, "list_strategies"):
            return {"applied": 0}

        applied = 0

        # 获取所有 incubating 策略
        try:
            incubating = await db.list_strategies("incubating", limit=500)
        except Exception:
            return {"applied": 0}

        for strategy in incubating:
            sid = str(strategy.get("id") or "").strip()
            family = str(
                strategy.get("strategy_type") or ""
            ).strip().lower()

            if not sid or not family:
                continue

            control_mode = None
            if family in families_to_freeze:
                control_mode = "frozen"
            elif family in families_to_cooldown:
                control_mode = "throttled"

            if control_mode and hasattr(db, "save_strategy_runtime_control"):
                try:
                    account = None
                    if hasattr(db, "get_strategy_incubation_account"):
                        account = await db.get_strategy_incubation_account(sid)
                    account_id = str((account or {}).get("account_id") or "").strip() or None
                    action = "freeze" if control_mode == "frozen" else "cooldown"
                    action_summary = {
                        "family": family,
                        "action": action,
                        "control_mode": control_mode,
                    }
                    await db.save_strategy_runtime_control({
                        "strategy_id": sid,
                        "account_id": account_id,
                        "control_mode": control_mode,
                        "status": "active",
                        "trigger_event_type": "incubation_factory.feedback_control",
                        "reason": f"incubation_factory_feedback_{family}",
                        "source": "incubation_factory",
                        "action_summary": action_summary,
                        "metadata": {
                            "family": family,
                            "action": action,
                            "trigger_event_type": "incubation_factory.feedback_control",
                            "applied_at": datetime.now(timezone.utc).isoformat(),
                        },
                    })
                    applied += 1
                except Exception as exc:
                    logger.debug(
                        "FeedbackWriter: control action failed for %s: %s",
                        sid,
                        exc,
                    )

        return {"applied": applied}

    async def _write_summary_snapshot(
        self, db: Any, report: dict[str, Any]
    ) -> None:
        """写入汇总快照供前端展示。"""
        if not hasattr(db, "save_strategy_closure_snapshot"):
            return
        try:
            await db.save_strategy_closure_snapshot({
                "strategy_id": "__incubation_factory__",
                "snapshot_type": "incubation_factory_daily_report",
                "as_of": report.get("report_date", str(date.today())),
                "metadata": {
                    "report_date": report.get("report_date"),
                    "generated_at": report.get("generated_at"),
                },
                "snapshot": {
                    "summary": report.get("summary"),
                    "hit_rate_dashboard": report.get("hit_rate_dashboard"),
                    "feedback_actions": report.get("feedback_actions"),
                },
            })
        except Exception as exc:
            logger.debug("FeedbackWriter: summary snapshot write failed: %s", exc)
