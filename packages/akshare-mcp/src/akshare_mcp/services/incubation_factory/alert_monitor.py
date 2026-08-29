"""孵化工厂 · 异常告警模块。

负责监控孵化工厂的运行健康状态，在异常情况下发出告警：
- 连续 3 天无运行
- 批量失败率 > 20%
- 策略堆积（incubating 数量异常增长）
- 晋升率异常低
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 告警阈值
ALERT_NO_RUN_DAYS = 3  # 连续无运行天数
ALERT_FAILURE_RATE_THRESHOLD = 0.20  # 批量失败率阈值
ALERT_STRATEGY_BACKLOG_THRESHOLD = 100  # 策略堆积阈值
ALERT_ZERO_PROMOTION_DAYS = 14  # 连续无晋升天数


class AlertMonitor:
    """孵化工厂异常告警监控器。

    检查运行健康状态并生成告警事件。
    """

    async def check(self, db: Any, run_result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """
        执行健康检查并生成告警。

        Args:
            db: 数据库连接
            run_result: 最近一次运行结果（可选）

        Returns:
            告警检查结果
        """
        alerts: list[dict[str, Any]] = []

        # 检查 1: 连续无运行
        no_run_alert = await self._check_no_run(db)
        if no_run_alert:
            alerts.append(no_run_alert)

        # 检查 2: 批量失败率
        if run_result:
            failure_alert = self._check_failure_rate(run_result)
            if failure_alert:
                alerts.append(failure_alert)

        # 检查 3: 策略堆积
        backlog_alert = await self._check_strategy_backlog(db)
        if backlog_alert:
            alerts.append(backlog_alert)

        # 检查 4: 连续无晋升
        no_promotion_alert = await self._check_no_promotion(db)
        if no_promotion_alert:
            alerts.append(no_promotion_alert)

        # 持久化告警
        if alerts:
            await self._persist_alerts(db, alerts)

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "alert_count": len(alerts),
            "alerts": alerts,
        }

    async def _check_no_run(self, db: Any) -> Optional[dict[str, Any]]:
        """检查是否连续多天无运行。"""
        if not hasattr(db, "list_strategy_domain_events"):
            return None

        try:
            events = await db.list_strategy_domain_events(
                event_type="incubation_factory.heartbeat",
                limit=1,
            )
            if not events:
                return {
                    "type": "no_run",
                    "severity": "warning",
                    "message": "孵化工厂从未运行过",
                    "detail": {"last_heartbeat": None},
                }

            last_event = events[0]
            payload = dict(last_event.get("payload") or {})
            timestamp_str = str(
                payload.get("timestamp")
                or last_event.get("created_at")
                or ""
            ).strip()

            if not timestamp_str:
                return None

            # 解析时间
            try:
                last_run = datetime.fromisoformat(
                    timestamp_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                return None

            now = datetime.now(timezone.utc)
            days_since = (now - last_run).days

            if days_since >= ALERT_NO_RUN_DAYS:
                return {
                    "type": "no_run",
                    "severity": "critical" if days_since >= 7 else "warning",
                    "message": f"孵化工厂已连续 {days_since} 天未运行",
                    "detail": {
                        "last_heartbeat": timestamp_str,
                        "days_since": days_since,
                        "threshold": ALERT_NO_RUN_DAYS,
                    },
                }
        except Exception as exc:
            logger.debug("AlertMonitor: no_run check failed: %s", exc)

        return None

    def _check_failure_rate(self, run_result: dict[str, Any]) -> Optional[dict[str, Any]]:
        """检查批量失败率。"""
        verification = dict(run_result.get("verification") or {})
        total = int(verification.get("total") or 0)
        errors = int(verification.get("errors") or 0)

        if total == 0:
            return None

        failure_rate = errors / total
        if failure_rate > ALERT_FAILURE_RATE_THRESHOLD:
            return {
                "type": "high_failure_rate",
                "severity": "warning",
                "message": f"批量验证失败率 {failure_rate:.0%} 超过阈值 {ALERT_FAILURE_RATE_THRESHOLD:.0%}",
                "detail": {
                    "total": total,
                    "errors": errors,
                    "failure_rate": round(failure_rate, 4),
                    "threshold": ALERT_FAILURE_RATE_THRESHOLD,
                },
            }

        return None

    async def _check_strategy_backlog(self, db: Any) -> Optional[dict[str, Any]]:
        """检查策略堆积。"""
        if not hasattr(db, "list_strategies"):
            return None

        try:
            incubating = await db.list_strategies("incubating", limit=500)
            count = len(incubating)

            if count >= ALERT_STRATEGY_BACKLOG_THRESHOLD:
                return {
                    "type": "strategy_backlog",
                    "severity": "warning",
                    "message": f"孵化中策略数 {count} 超过阈值 {ALERT_STRATEGY_BACKLOG_THRESHOLD}",
                    "detail": {
                        "incubating_count": count,
                        "threshold": ALERT_STRATEGY_BACKLOG_THRESHOLD,
                    },
                }
        except Exception as exc:
            logger.debug("AlertMonitor: backlog check failed: %s", exc)

        return None

    async def _check_no_promotion(self, db: Any) -> Optional[dict[str, Any]]:
        """检查连续无晋升。"""
        if not hasattr(db, "list_strategy_domain_events"):
            return None

        try:
            events = await db.list_strategy_domain_events(
                event_type="incubation.stage_transitioned",
                limit=20,
            )

            # 查找最近一次晋升事件
            last_promotion = None
            for event in events:
                payload = dict(event.get("payload") or {})
                if payload.get("to_stage") in ("graduation_ready", "promoted"):
                    created = str(event.get("created_at") or "").strip()
                    if created:
                        try:
                            last_promotion = datetime.fromisoformat(
                                created.replace("Z", "+00:00")
                            )
                            break
                        except (ValueError, TypeError):
                            continue

            if last_promotion is None:
                # 没有任何晋升记录，检查是否有足够的孵化策略
                if hasattr(db, "list_strategies"):
                    incubating = await db.list_strategies("incubating", limit=10)
                    if len(incubating) >= 5:
                        return {
                            "type": "no_promotion",
                            "severity": "info",
                            "message": "尚无策略晋升记录（孵化中策略可能仍在预热期）",
                            "detail": {"last_promotion": None, "incubating_count": len(incubating)},
                        }
                return None

            now = datetime.now(timezone.utc)
            days_since = (now - last_promotion).days

            if days_since >= ALERT_ZERO_PROMOTION_DAYS:
                return {
                    "type": "no_promotion",
                    "severity": "warning",
                    "message": f"已连续 {days_since} 天无策略晋升",
                    "detail": {
                        "last_promotion": last_promotion.isoformat(),
                        "days_since": days_since,
                        "threshold": ALERT_ZERO_PROMOTION_DAYS,
                    },
                }
        except Exception as exc:
            logger.debug("AlertMonitor: no_promotion check failed: %s", exc)

        return None

    async def _persist_alerts(self, db: Any, alerts: list[dict[str, Any]]) -> None:
        """持久化告警事件。"""
        if not hasattr(db, "save_strategy_domain_event"):
            return

        for alert in alerts:
            try:
                await db.save_strategy_domain_event({
                    "strategy_id": None,
                    "aggregate_type": "incubation_factory",
                    "aggregate_id": f"alert_{alert['type']}_{date.today()}",
                    "event_type": f"incubation_factory.alert.{alert['type']}",
                    "source": "incubation_factory_alert_monitor",
                    "severity": alert.get("severity", "warning"),
                    "payload": alert,
                })
            except Exception as exc:
                logger.debug("AlertMonitor: persist alert failed: %s", exc)
