"""孵化工厂 · 加速孵化模块。

实现"加速孵化"机制：连续 N 天 promote 决策的策略可以提前毕业，
无需等待完整的最短孵化期。

加速条件：
- 连续 10 天 decision='promote'
- primary_skill_lcb > 0.03（技能下界显著为正）
- stability_gap <= 0.03（极其稳定）
- coverage_ratio >= 0.80（高覆盖率）
- 无未解决风险事件
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 加速孵化阈值
ACCELERATE_CONSECUTIVE_PROMOTE_DAYS = 10
ACCELERATE_SKILL_LCB_MIN = 0.03
ACCELERATE_STABILITY_GAP_MAX = 0.03
ACCELERATE_COVERAGE_RATIO_MIN = 0.80


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _safe_float(value: Any, default: float = 0.0) -> float:
    numeric = _finite_float(value)
    if numeric is not None:
        return numeric
    fallback = _finite_float(default)
    return fallback if fallback is not None else 0.0


def _safe_int(value: Any, default: int = 0) -> int:
    numeric = _finite_float(value)
    if numeric is not None:
        return int(numeric)
    fallback = _finite_float(default)
    return int(fallback if fallback is not None else 0)


class IncubationAccelerator:
    """加速孵化评估器。

    检查策略是否满足加速孵化条件，如果满足则触发提前晋升。
    """

    async def evaluate_batch(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
        verifications: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """
        批量评估策略是否可以加速孵化。

        Args:
            db: 数据库连接
            strategies: 孵化中的策略列表
            verifications: {strategy_id: verification_result} 映射

        Returns:
            加速评估结果
        """
        accelerated: list[dict[str, Any]] = []
        evaluated = 0

        for strategy in strategies:
            sid = str(strategy.get("id") or "").strip()
            if not sid:
                continue

            verification = verifications.get(sid)
            if not verification:
                continue

            evaluated += 1
            result = await self._evaluate_single(db, strategy, verification)
            if result.get("eligible"):
                accelerated.append(result)

                # 触发加速晋升
                if hasattr(db, "save_strategy_domain_event"):
                    await self._trigger_acceleration(db, strategy, result)

        return {
            "evaluated": evaluated,
            "accelerated_count": len(accelerated),
            "accelerated": accelerated,
        }

    async def _evaluate_single(
        self,
        db: Any,
        strategy: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        """评估单个策略是否满足加速条件。"""
        sid = str(strategy.get("id") or "").strip()

        # 检查验证指标
        primary_skill_lcb = _safe_float(verification.get("primary_skill_lcb"), 0.0)
        recent_skill_lcb = _safe_float(verification.get("recent_primary_skill_lcb"), 0.0)
        stability_gap = _safe_float(verification.get("stability_gap"), 1.0)
        coverage_ratio = _safe_float(verification.get("coverage_ratio"), 0.0)
        primary_n = _safe_int(verification.get("primary_effective_n"), 0)

        # 基本指标门槛
        if primary_skill_lcb < ACCELERATE_SKILL_LCB_MIN:
            return {"strategy_id": sid, "eligible": False, "reason": "skill_lcb_too_low"}
        if recent_skill_lcb < ACCELERATE_SKILL_LCB_MIN:
            return {"strategy_id": sid, "eligible": False, "reason": "recent_skill_lcb_too_low"}
        if stability_gap > ACCELERATE_STABILITY_GAP_MAX:
            return {"strategy_id": sid, "eligible": False, "reason": "stability_gap_too_high"}
        if coverage_ratio < ACCELERATE_COVERAGE_RATIO_MIN:
            return {"strategy_id": sid, "eligible": False, "reason": "coverage_too_low"}
        if primary_n < 20:
            return {"strategy_id": sid, "eligible": False, "reason": "insufficient_samples"}

        # 检查连续 promote 天数
        promote_streak = await self._get_promote_streak(db, sid)
        if promote_streak < ACCELERATE_CONSECUTIVE_PROMOTE_DAYS:
            return {
                "strategy_id": sid,
                "eligible": False,
                "reason": "insufficient_promote_streak",
                "promote_streak": promote_streak,
                "required": ACCELERATE_CONSECUTIVE_PROMOTE_DAYS,
            }

        # 检查是否有未解决风险事件
        open_risks = 0
        if hasattr(db, "list_strategy_runtime_risk_events"):
            try:
                risk_events = await db.list_strategy_runtime_risk_events(
                    strategy_id=sid, status="open", limit=5
                )
                open_risks = len(risk_events)
            except Exception:
                pass

        if open_risks > 0:
            return {"strategy_id": sid, "eligible": False, "reason": "open_risk_events"}

        # 所有条件满足 → 可以加速
        return {
            "strategy_id": sid,
            "strategy_name": strategy.get("name"),
            "eligible": True,
            "reason": "all_conditions_met",
            "promote_streak": promote_streak,
            "primary_skill_lcb": primary_skill_lcb,
            "recent_skill_lcb": recent_skill_lcb,
            "stability_gap": stability_gap,
            "coverage_ratio": coverage_ratio,
            "primary_n": primary_n,
        }

    async def _get_promote_streak(self, db: Any, strategy_id: str) -> int:
        """获取策略连续 promote 决策的天数。"""
        if not hasattr(db, "list_strategy_incubation_metrics"):
            return 0

        try:
            metrics = await db.list_strategy_incubation_metrics(
                strategy_id, limit=ACCELERATE_CONSECUTIVE_PROMOTE_DAYS + 5
            )
            streak = 0
            for metric in metrics:
                decision = str(metric.get("decision") or "").strip().lower()
                if decision == "promote":
                    streak += 1
                else:
                    break
            return streak
        except Exception:
            return 0

    async def _trigger_acceleration(
        self,
        db: Any,
        strategy: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> None:
        """触发加速晋升事件。"""
        sid = str(strategy.get("id") or "").strip()

        try:
            await db.save_strategy_domain_event({
                "strategy_id": sid,
                "aggregate_type": "incubation_factory",
                "aggregate_id": sid,
                "event_type": "incubation_factory.acceleration_triggered",
                "source": "incubation_factory_accelerator",
                "severity": "info",
                "payload": {
                    "strategy_name": strategy.get("name"),
                    "strategy_type": strategy.get("strategy_type"),
                    "promote_streak": evaluation.get("promote_streak"),
                    "primary_skill_lcb": evaluation.get("primary_skill_lcb"),
                    "stability_gap": evaluation.get("stability_gap"),
                    "coverage_ratio": evaluation.get("coverage_ratio"),
                    "triggered_at": datetime.now(timezone.utc).isoformat(),
                },
            })
            logger.info(
                "IncubationAccelerator: triggered acceleration for %s (%s) "
                "with %d-day promote streak",
                sid,
                strategy.get("name"),
                evaluation.get("promote_streak"),
            )
        except Exception as exc:
            logger.debug("IncubationAccelerator: event save failed: %s", exc)
            return

        try:
            from ..incubation_pipeline import get_strategy_incubation_pipeline_service

            await get_strategy_incubation_pipeline_service().run_strategy(
                db,
                strategy,
                source="incubation_factory_accelerator",
                auto_apply_review=True,
            )
        except Exception as exc:
            logger.warning(
                "IncubationAccelerator: pipeline trigger failed for %s: %s",
                sid,
                exc,
            )
