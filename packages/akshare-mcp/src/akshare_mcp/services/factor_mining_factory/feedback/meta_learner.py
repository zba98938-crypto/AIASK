"""Meta-Learner — 从历史挖掘结果中学习成功因子的结构特征。

参考：AlphaAgentEvo (ICLR 2026) — 自进化 Agentic RL
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class FactorMetaLearner:
    """元学习器 — 分析历史挖掘周期，提取成功模式。

    输入：历史所有候选因子的 (结构特征, 验证结果) 对
    输出：搜索方向建议 + 引擎效率统计
    """

    def __init__(self):
        self._cycle_history: list[dict[str, Any]] = []
        self._success_patterns: dict[str, int] = Counter()
        self._failure_patterns: dict[str, int] = Counter()
        self._engine_stats: dict[str, dict[str, int]] = {}

    async def record_cycle(
        self,
        *,
        run_id: str,
        raw_count: int,
        evolved_count: int,
        validated_count: int,
        admitted_count: int,
        candidates: list[Any] | None = None,
    ):
        """记录一次挖掘周期的结果。"""
        record = {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw_count": raw_count,
            "evolved_count": evolved_count,
            "validated_count": validated_count,
            "admitted_count": admitted_count,
            "pass_rate": validated_count / max(1, raw_count),
            "admit_rate": admitted_count / max(1, validated_count) if validated_count > 0 else 0.0,
        }
        self._cycle_history.append(record)

        # 统计引擎效率
        if candidates:
            for candidate in candidates:
                engine = getattr(candidate, "generation_engine", "") or "unknown"
                if engine not in self._engine_stats:
                    self._engine_stats[engine] = {"generated": 0, "validated": 0}
                self._engine_stats[engine]["generated"] += 1
                outcome = self._candidate_outcome(candidate)
                pattern_key = self._pattern_key(candidate)
                if outcome is True:
                    self._engine_stats[engine]["validated"] += 1
                    self._success_patterns[pattern_key] += 1
                elif outcome is False:
                    self._failure_patterns[pattern_key] += 1

        # 保留最近 100 条记录
        if len(self._cycle_history) > 100:
            self._cycle_history = self._cycle_history[-100:]

        logger.info(
            "MetaLearner: recorded cycle %s (pass_rate=%.2f admit_rate=%.2f)",
            run_id,
            record["pass_rate"],
            record["admit_rate"],
        )

    def get_recommendations(self) -> dict[str, Any]:
        """获取搜索方向建议。"""
        if not self._cycle_history:
            return {"status": "insufficient_data", "recommendations": []}

        # 分析引擎效率
        engine_efficiency = {}
        for engine, stats in self._engine_stats.items():
            generated = stats.get("generated", 0)
            validated = stats.get("validated", 0)
            efficiency = validated / max(1, generated)
            engine_efficiency[engine] = {
                "generated": generated,
                "validated": validated,
                "efficiency": round(efficiency, 4),
            }

        # 分析趋势
        recent = self._cycle_history[-10:]
        avg_pass_rate = sum(r["pass_rate"] for r in recent) / len(recent)
        avg_admit_rate = sum(r["admit_rate"] for r in recent) / len(recent)

        recommendations = []
        if avg_pass_rate < 0.2:
            recommendations.append("增加 GP 引擎预算（当前通过率低，需要更广泛搜索）")
        if avg_admit_rate < 0.3:
            recommendations.append("加强新颖性压力（入池率低，可能生成过多重复因子）")

        # 推荐效率最高的引擎
        best_engine = max(engine_efficiency.items(), key=lambda x: x[1]["efficiency"], default=None)
        if best_engine:
            recommendations.append(f"优先使用 {best_engine[0]} 引擎（效率最高: {best_engine[1]['efficiency']:.2%}）")

        return {
            "status": "ready",
            "cycle_count": len(self._cycle_history),
            "avg_pass_rate": round(avg_pass_rate, 4),
            "avg_admit_rate": round(avg_admit_rate, 4),
            "engine_efficiency": engine_efficiency,
            "recommendations": recommendations,
        }

    def get_pattern_memory(self) -> dict[str, Any]:
        """Expose successful and failed generation patterns to the next cycle."""

        return {
            "successful_pattern_memory": self._counter_rows(self._success_patterns),
            "failed_pattern_memory": self._counter_rows(self._failure_patterns),
        }

    @staticmethod
    def _counter_rows(counter: Counter) -> list[dict[str, Any]]:
        return [
            {"pattern": pattern, "count": int(count)}
            for pattern, count in counter.most_common(20)
        ]

    @staticmethod
    def _pattern_key(candidate: Any) -> str:
        blueprint_id = str(getattr(candidate, "blueprint_id", "") or "").strip()
        if blueprint_id:
            return blueprint_id
        trace = dict(getattr(candidate, "generation_trace", None) or {})
        if trace.get("blueprint_id"):
            return str(trace.get("blueprint_id"))
        family = str(
            getattr(candidate, "factor_family", "")
            or getattr(candidate, "family", "")
            or "custom"
        )
        return family

    @staticmethod
    def _candidate_succeeded(candidate: Any) -> bool:
        outcome = FactorMetaLearner._candidate_outcome(candidate)
        return bool(outcome)

    @staticmethod
    def _candidate_outcome(candidate: Any) -> bool | None:
        """Return conclusive strict outcome; quick-only evidence is not success."""

        trace = dict(getattr(candidate, "generation_trace", None) or {})
        strict_attempted = bool(trace.get("strict_validation_attempted"))
        result = getattr(candidate, "validation_result", None)
        if isinstance(result, dict) and result:
            try:
                from ..quality import evaluate_validation_evidence

                evidence_passed = bool(evaluate_validation_evidence(result).get("passed"))
            except Exception:
                evidence = dict(result.get("quality_evidence") or {})
                evidence_passed = bool(evidence.get("passed"))
            rating = dict(result.get("rating") or {})
            governance = dict(rating.get("governance") or {})
            grade = str(rating.get("grade") or "")
            return bool(
                result.get("success")
                and evidence_passed
                and grade in {"A", "B"}
                and not bool(governance.get("admission_blocked"))
            )

        if strict_attempted:
            return False

        quick = dict(getattr(candidate, "quick_evidence", None) or {})
        if quick:
            if quick.get("passed") is False:
                return False
            return None
        return None
