"""孵化工厂 · 命中率报告模块。

负责生成孵化工厂的命中率报告，体现 AI 生成策略的真实胜率。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


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


class HitRateReporter:
    """命中率报告生成器。

    聚合所有孵化中策略的命中率数据，生成：
    - 整体命中率
    - 按 family 分组的命中率
    - 按孵化阶段分组的命中率
    - 趋势分析（命中率是否在改善）
    """

    async def generate(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
        verifications: dict[str, dict[str, Any]],
        pipeline_result: dict[str, Any],
        *,
        report_date: Optional[date] = None,
        trade_prediction_result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        生成命中率报告。

        Args:
            db: 数据库连接
            strategies: 孵化中的策略列表
            verifications: {strategy_id: verification_result} 映射
            pipeline_result: 孵化流水线批量运行结果
            report_date: 报告日期

        Returns:
            完整的命中率报告
        """
        today = report_date or date.today()

        # 聚合整体命中率
        overall = self._aggregate_overall(verifications)

        # 按 family 分组
        by_family = self._aggregate_by_family(strategies, verifications)

        # 按阶段分组
        by_stage = self._aggregate_by_stage(strategies, verifications, pipeline_result)

        # 趋势分析
        trend = await self._compute_trend(db, strategies)

        # P3-1：命中率矩阵（strategy_type × regime × holding_bucket），复用已算的 verifications
        hit_rate_matrix = self._aggregate_matrix(strategies, verifications)

        # 反馈建议
        feedback_actions = self._derive_feedback_actions(by_family)
        trade_prediction_dashboard = await self._build_trade_prediction_dashboard(
            db,
            trade_prediction_result=trade_prediction_result,
        )
        feedback_actions["prediction_feedback"] = self._derive_prediction_feedback_actions(
            trade_prediction_dashboard
        )

        report = {
            "report_date": str(today),
            "run_source": "incubation_factory",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_incubating": len(strategies),
                "total_with_signals": sum(
                    1
                    for v in verifications.values()
                    if _safe_int(v.get("primary_effective_n"), 0) > 0
                ),
                "auto_promoted": _safe_int(pipeline_result.get("auto_promoted"), 0),
                "stage_counts": dict(pipeline_result.get("stage_counts") or {}),
            },
            "hit_rate_dashboard": {
                "overall": overall,
                "by_family": by_family,
                "by_stage": by_stage,
                "trend": trend,
                # P3-1：type × regime × bucket 矩阵（空单元诚实标注 insufficient_samples）
                "matrix": hit_rate_matrix,
                "trade_predictions": trade_prediction_dashboard,
            },
            "trade_prediction_dashboard": trade_prediction_dashboard,
            "feedback_actions": feedback_actions,
        }

        # 持久化报告
        await self._persist_report(db, report)

        return report

    async def _build_trade_prediction_dashboard(
        self,
        db: Any,
        *,
        trade_prediction_result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Build read-only trade prediction diagnostics for reports/UI."""

        summary: dict[str, Any] = {}
        matrix: dict[str, Any] = {"rows": [], "row_count": 0}
        if hasattr(db, "summarize_strategy_trade_predictions"):
            try:
                summary = await db.summarize_strategy_trade_predictions(limit=1000)
            except Exception as exc:  # noqa: BLE001
                logger.debug("HitRateReporter: prediction summary failed: %s", exc)
                summary = {}
        if hasattr(db, "aggregate_trade_prediction_matrix"):
            try:
                matrix = await db.aggregate_trade_prediction_matrix(limit=1000)
            except Exception as exc:  # noqa: BLE001
                logger.debug("HitRateReporter: prediction matrix failed: %s", exc)
                matrix = {"rows": [], "row_count": 0, "error": str(exc)}

        phase_result = dict(trade_prediction_result or {})
        if not summary:
            status_counts = dict(phase_result.get("score_status_counts") or {})
            summary = {
                "object": "trade_prediction.status",
                "prediction_count": None,
                "outcome_count": None,
                "sample_n": _safe_int(phase_result.get("evaluated"), 0),
                "pending_count": None,
                "evaluated_count": _safe_int(phase_result.get("evaluated"), 0),
                "partial_count": _safe_int(status_counts.get("partial_daily_only"), 0)
                + _safe_int(status_counts.get("partial_intraday_missing"), 0),
                "score_status_counts": status_counts,
                "data_quality_status_counts": dict(phase_result.get("data_quality_status_counts") or {}),
                "score_version_counts": {},
                "score_distribution": {},
            }

        return {
            "summary": summary,
            "matrix": matrix,
            "phase_result": {
                "status": phase_result.get("status"),
                "score_version": phase_result.get("score_version"),
                "intraday_score_version": phase_result.get("intraday_score_version"),
                "evaluated": _safe_int(phase_result.get("evaluated"), 0),
                "intraday_evaluated": _safe_int(phase_result.get("intraday_evaluated"), 0),
                "score_status_counts": dict(phase_result.get("score_status_counts") or {}),
                "data_quality_status_counts": dict(phase_result.get("data_quality_status_counts") or {}),
                "intraday_sync": dict(phase_result.get("intraday_sync") or {}),
            },
        }

    def _derive_prediction_feedback_actions(
        self,
        dashboard: dict[str, Any],
    ) -> dict[str, Any]:
        """Create diagnostic-only suggestions from prediction scoring."""

        summary = dict((dashboard or {}).get("summary") or {})
        matrix = dict((dashboard or {}).get("matrix") or {})
        rows = list(matrix.get("rows") or [])
        sample_n = _safe_int(summary.get("sample_n"), 0)
        partial_count = _safe_int(summary.get("partial_count"), 0)
        suggestions: list[dict[str, Any]] = []
        if sample_n < 30:
            suggestions.append({
                "action": "observe",
                "reason": f"insufficient_samples:{sample_n}<30",
                "hard_gate": False,
            })
        if partial_count > 0:
            suggestions.append({
                "action": "repair_data",
                "reason": f"partial_prediction_outcomes:{partial_count}",
                "hard_gate": False,
            })
        for row in rows:
            if not isinstance(row, dict):
                continue
            score = row.get("score_avg")
            lcb = row.get("score_lcb_95")
            sample = _safe_int(row.get("sample_n"), 0)
            if sample < 10 or score is None:
                continue
            score_value = _finite_float(score)
            lcb_value = _safe_float(lcb, 0.0)
            if score_value is None:
                continue
            action = "boost" if score_value >= 0.70 and lcb_value >= 0.55 else "cool"
            suggestions.append({
                "action": action,
                "dimension": row.get("dimension"),
                "value": row.get("value"),
                "sample_n": sample,
                "score_avg": score,
                "score_lcb_95": lcb,
                "hard_gate": False,
            })
        return {
            "enabled_for_controls": False,
            "suggestions": suggestions[:50],
            "sample_n": sample_n,
            "partial_count": partial_count,
        }

    def _aggregate_matrix(
        self,
        strategies: list[dict[str, Any]],
        verifications: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """P3-1：把已算的 verifications 交叉聚合成 type × regime × bucket 矩阵。"""
        try:
            from .hit_rate_matrix import aggregate_hit_rate_matrix

            strategies_by_id = {
                str((s or {}).get("id") or "").strip(): dict(s)
                for s in (strategies or [])
                if str((s or {}).get("id") or "").strip()
            }
            verify_results: list[dict[str, Any]] = []
            for sid, result in (verifications or {}).items():
                if not isinstance(result, dict):
                    continue
                item = dict(result)
                item.setdefault("strategy_id", sid)
                verify_results.append(item)
            return aggregate_hit_rate_matrix(verify_results, strategies_by_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HitRateReporter: matrix aggregation failed: %s", exc)
            return {"matrix": {}, "totals": {}, "error": str(exc)}

    def _aggregate_overall(
        self, verifications: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """聚合整体命中率。"""
        total_signals = 0
        total_hits = 0
        all_skill_lcbs: list[float] = []
        all_sharpes: list[float] = []

        for v in verifications.values():
            n = _safe_int(v.get("primary_effective_n"), 0)
            hit_rate = _safe_float(v.get("primary_hit_rate"), 0.0)
            skill_lcb = _safe_float(v.get("primary_skill_lcb"), 0.0)
            forward_sharpe = _safe_float(v.get("forward_sharpe"), 0.0)

            if n > 0:
                total_signals += n
                total_hits += _safe_int(round(hit_rate * n), 0)
                all_skill_lcbs.append(skill_lcb)
                all_sharpes.append(forward_sharpe)

        overall_hit_rate = total_hits / total_signals if total_signals > 0 else 0.0
        avg_skill_lcb = (
            sum(all_skill_lcbs) / len(all_skill_lcbs) if all_skill_lcbs else 0.0
        )
        avg_sharpe = (
            sum(all_sharpes) / len(all_sharpes) if all_sharpes else 0.0
        )

        return {
            "total_signals": total_signals,
            "hit_count": total_hits,
            "hit_rate": round(overall_hit_rate, 4),
            "avg_skill_lcb": round(avg_skill_lcb, 4),
            "avg_forward_sharpe": round(avg_sharpe, 4),
            "strategy_count": len(
                [v for v in verifications.values() if _safe_int(v.get("primary_effective_n"), 0) > 0]
            ),
        }

    def _aggregate_by_family(
        self,
        strategies: list[dict[str, Any]],
        verifications: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """按策略 family 分组聚合命中率。"""
        family_data: dict[str, dict[str, list]] = defaultdict(
            lambda: {"hit_rates": [], "skill_lcbs": [], "ns": [], "sharpes": []}
        )

        for strategy in strategies:
            sid = str(strategy.get("id") or "").strip()
            family = str(
                strategy.get("strategy_type") or strategy.get("candidate_family") or "unknown"
            ).strip().lower()
            v = verifications.get(sid, {})
            n = _safe_int(v.get("primary_effective_n"), 0)
            if n > 0:
                family_data[family]["hit_rates"].append(
                    _safe_float(v.get("primary_hit_rate"), 0.0)
                )
                family_data[family]["skill_lcbs"].append(
                    _safe_float(v.get("primary_skill_lcb"), 0.0)
                )
                family_data[family]["ns"].append(n)
                family_data[family]["sharpes"].append(
                    _safe_float(v.get("forward_sharpe"), 0.0)
                )

        result = {}
        for family, data in sorted(family_data.items()):
            if not data["hit_rates"]:
                continue
            total_n = sum(data["ns"])
            # 加权平均命中率
            weighted_hit_rate = sum(
                hr * n for hr, n in zip(data["hit_rates"], data["ns"])
            ) / total_n if total_n > 0 else 0.0

            result[family] = {
                "hit_rate": round(weighted_hit_rate, 4),
                "avg_skill_lcb": round(
                    sum(data["skill_lcbs"]) / len(data["skill_lcbs"]), 4
                ),
                "avg_forward_sharpe": round(
                    sum(data["sharpes"]) / len(data["sharpes"]), 4
                ),
                "total_n": total_n,
                "strategy_count": len(data["hit_rates"]),
            }

        return result

    def _aggregate_by_stage(
        self,
        strategies: list[dict[str, Any]],
        verifications: dict[str, dict[str, Any]],
        pipeline_result: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """按孵化阶段分组聚合命中率。"""
        # 从 pipeline_result 中提取每个策略的阶段
        stage_map: dict[str, str] = {}
        for item in list(pipeline_result.get("items") or []):
            sid = str(item.get("strategy_id") or "").strip()
            snapshot = dict(item.get("snapshot") or {})
            stage = str(snapshot.get("pipeline_stage") or "unknown")
            if sid:
                stage_map[sid] = stage

        stage_data: dict[str, dict[str, list]] = defaultdict(
            lambda: {"hit_rates": [], "skill_lcbs": []}
        )

        for strategy in strategies:
            sid = str(strategy.get("id") or "").strip()
            stage = stage_map.get(sid, "unknown")
            v = verifications.get(sid, {})
            n = _safe_int(v.get("primary_effective_n"), 0)
            if n > 0:
                stage_data[stage]["hit_rates"].append(
                    _safe_float(v.get("primary_hit_rate"), 0.0)
                )
                stage_data[stage]["skill_lcbs"].append(
                    _safe_float(v.get("primary_skill_lcb"), 0.0)
                )

        result = {}
        for stage, data in sorted(stage_data.items()):
            if not data["hit_rates"]:
                continue
            result[stage] = {
                "avg_hit_rate": round(
                    sum(data["hit_rates"]) / len(data["hit_rates"]), 4
                ),
                "avg_skill_lcb": round(
                    sum(data["skill_lcbs"]) / len(data["skill_lcbs"]), 4
                ),
                "strategy_count": len(data["hit_rates"]),
            }

        return result

    async def _compute_trend(
        self, db: Any, strategies: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """计算命中率趋势（最近 7 天 vs 之前）。"""
        if not hasattr(db, "list_strategy_incubation_metrics"):
            return {"available": False}

        recent_skill_lcbs: list[float] = []
        older_skill_lcbs: list[float] = []

        for strategy in strategies[:50]:  # 限制查询量
            sid = str(strategy.get("id") or "").strip()
            if not sid:
                continue
            try:
                metrics = await db.list_strategy_incubation_metrics(sid, limit=14)
                if len(metrics) >= 7:
                    recent = metrics[:7]
                    older = metrics[7:]
                    for m in recent:
                        lcb = m.get("skill_lcb_5d")
                        numeric = _finite_float(lcb)
                        if numeric is not None:
                            recent_skill_lcbs.append(numeric)
                    for m in older:
                        lcb = m.get("skill_lcb_5d")
                        numeric = _finite_float(lcb)
                        if numeric is not None:
                            older_skill_lcbs.append(numeric)
            except Exception:
                continue

        if not recent_skill_lcbs or not older_skill_lcbs:
            return {"available": False}

        recent_avg = sum(recent_skill_lcbs) / len(recent_skill_lcbs)
        older_avg = sum(older_skill_lcbs) / len(older_skill_lcbs)
        improvement = recent_avg - older_avg

        return {
            "available": True,
            "recent_7d_avg_skill_lcb": round(recent_avg, 4),
            "prior_7d_avg_skill_lcb": round(older_avg, 4),
            "improvement": round(improvement, 4),
            "direction": "improving" if improvement > 0.005 else (
                "declining" if improvement < -0.005 else "stable"
            ),
        }

    def _derive_feedback_actions(
        self, by_family: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """根据命中率数据推导反馈动作。"""
        families_to_boost: list[str] = []
        families_to_cooldown: list[str] = []
        families_to_freeze: list[str] = []

        for family, data in by_family.items():
            skill_lcb = _safe_float(data.get("avg_skill_lcb"), 0.0)
            hit_rate = _safe_float(data.get("hit_rate"), 0.0)
            n = _safe_int(data.get("total_n"), 0)

            # 样本不足时不做判断
            if n < 15:
                continue

            # 技能下界显著为正 → 增加配额
            if skill_lcb > 0.03 and hit_rate > 0.55:
                families_to_boost.append(family)
            # 技能下界为负 → 冷却
            elif skill_lcb < -0.02:
                families_to_cooldown.append(family)
            # 技能下界严重为负 → 冻结
            elif skill_lcb < -0.05:
                families_to_freeze.append(family)

        return {
            "families_to_boost": families_to_boost,
            "families_to_cooldown": families_to_cooldown,
            "families_to_freeze": families_to_freeze,
        }

    async def _persist_report(self, db: Any, report: dict[str, Any]) -> None:
        """持久化命中率报告。"""
        if not hasattr(db, "save_strategy_domain_event"):
            return
        try:
            await db.save_strategy_domain_event({
                "strategy_id": None,
                "aggregate_type": "incubation_factory",
                "aggregate_id": f"hit_rate_report_{report['report_date']}",
                "event_type": "incubation_factory.hit_rate_report_generated",
                "source": "incubation_factory",
                "severity": "info",
                "payload": report,
            })
        except Exception as exc:
            logger.debug("HitRateReporter: persist report failed: %s", exc)
