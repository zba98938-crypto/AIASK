"""多引擎调度器 — 根据市场状态和历史表现动态分配搜索预算。"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .base import FactorCandidate, SearchBudget, SearchEngine

logger = logging.getLogger(__name__)


class EngineScheduler:
    """多引擎调度器。

    预算分配策略：
    - 探索期（因子池 < 50）：LLM 40% + GP 30% + MCTS 20% + Rule 10%
    - 稳定期（因子池 50-200）：GP 35% + Rule 25% + LLM 20% + MCTS 20%
    - 衰减期（活跃因子衰减 > 30%）：LLM 30% + GP 30% + MCTS 30% + Rule 10%
    """

    def __init__(self):
        self._engines: dict[str, SearchEngine] = {}
        self._last_engines_used: list[str] = []
        self._quality_feedback: dict[str, deque[dict[str, int]]] = {}
        self._register_default_engines()

    @property
    def last_engines_used(self) -> list[str]:
        return list(self._last_engines_used)

    def _register_default_engines(self):
        """注册默认引擎。"""
        from .llm_engine import LLMSearchEngine
        from .gp_engine import GeneticProgrammingEngine
        from .mcts_engine import MCTSSearchEngine
        from .rl_engine import RLAlphaGenEngine
        from .rule_engine import RuleSeedEngine

        self._engines = {
            "llm_primary": LLMSearchEngine(engine_id="llm_primary"),
            "gp_classic": GeneticProgrammingEngine(engine_id="gp_classic"),
            "mcts_guided": MCTSSearchEngine(engine_id="mcts_guided"),
            "rl_alphagen": RLAlphaGenEngine(engine_id="rl_alphagen"),
            "rule_seed": RuleSeedEngine(engine_id="rule_seed"),
        }

    def register_engine(self, engine: SearchEngine):
        """注册自定义引擎。"""
        self._engines[engine.engine_id] = engine

    @staticmethod
    def _is_cpu_bound_engine(engine: SearchEngine) -> bool:
        return str(getattr(engine, "engine_id", "") or "") in {"gp_classic", "mcts_guided", "rl_alphagen"}

    @staticmethod
    def _run_engine_in_worker_thread(engine: SearchEngine, context: Any, budget: SearchBudget) -> list[FactorCandidate]:
        result_box: dict[str, Any] = {}
        error_box: dict[str, BaseException] = {}

        def _target() -> None:
            try:
                result_box["result"] = asyncio.run(engine.generate(context, budget))
            except BaseException as exc:  # noqa: BLE001 - surfaced to scheduler for uniform degradation
                error_box["error"] = exc

        worker = threading.Thread(target=_target, name=f"factor-engine-{engine.engine_id}", daemon=True)
        worker.start()
        worker.join(timeout=max(0.1, float(budget.max_time_sec or 0.1)))
        if worker.is_alive():
            raise TimeoutError(f"engine {engine.engine_id} exceeded {budget.max_time_sec:.1f}s")
        if error_box:
            raise error_box["error"]
        result = result_box.get("result")
        return result if isinstance(result, list) else []

    async def search(
        self,
        *,
        context: Any,
        engines: list[str] | None = None,
        candidate_count: int = 30,
    ) -> list[FactorCandidate]:
        """多引擎并行搜索。"""
        budgets = self._allocate_budgets(context, engines, candidate_count)
        self._last_engines_used = list(budgets.keys())

        tasks = []
        for engine_id, budget in budgets.items():
            engine = self._engines.get(engine_id)
            if engine is None:
                continue
            tasks.append(self._run_engine(engine, context, budget))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_candidates: list[FactorCandidate] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Engine failed: %s", result)
                continue
            if isinstance(result, list):
                all_candidates.extend(result)

        logger.info(
            "EngineScheduler: %d engines produced %d candidates",
            len(budgets),
            len(all_candidates),
        )
        return all_candidates

    async def _run_engine(
        self,
        engine: SearchEngine,
        context: Any,
        budget: SearchBudget,
    ) -> list[FactorCandidate]:
        """运行单个引擎（带超时保护）。"""
        try:
            if self._is_cpu_bound_engine(engine):
                return await asyncio.to_thread(self._run_engine_in_worker_thread, engine, context, budget)
            return await asyncio.wait_for(
                engine.generate(context, budget),
                timeout=budget.max_time_sec,
            )
        except TimeoutError:
            logger.warning("Engine %s timed out after %.1fs", engine.engine_id, budget.max_time_sec)
            return []
        except asyncio.TimeoutError:
            logger.warning("Engine %s timed out after %.1fs", engine.engine_id, budget.max_time_sec)
            return []
        except Exception as exc:
            logger.warning("Engine %s failed: %s", engine.engine_id, exc)
            return []

    def _allocate_budgets(
        self,
        context: Any,
        engines: list[str] | None,
        total_count: int,
    ) -> dict[str, SearchBudget]:
        """分配搜索预算。"""
        available = list(engines or self._engines.keys())
        available = [eid for eid in available if eid in self._engines]

        if not available:
            return {}

        pool_size = getattr(context, "active_pool_size", 0)
        decay_rate = getattr(context, "pool_decay_rate", 0.0)

        # 根据状态分配权重
        if pool_size < 50:
            # 探索期
            weights = {"llm_primary": 0.25, "gp_classic": 0.25, "mcts_guided": 0.20, "rl_alphagen": 0.15, "rule_seed": 0.15}
        elif decay_rate > 0.3:
            # 衰减期
            weights = {"llm_primary": 0.20, "gp_classic": 0.25, "mcts_guided": 0.25, "rl_alphagen": 0.20, "rule_seed": 0.10}
        else:
            # 稳定期
            weights = {"gp_classic": 0.25, "rl_alphagen": 0.25, "mcts_guided": 0.20, "llm_primary": 0.15, "rule_seed": 0.15}

        weights = self._apply_quality_feedback(weights)
        if engines:
            selected_total = sum(float(weights.get(engine_id, 0.0)) for engine_id in available)
            if selected_total > 0.0:
                weights = {
                    engine_id: float(weights.get(engine_id, 0.0)) / selected_total
                    for engine_id in available
                }
            else:
                weights = {engine_id: 1.0 / len(available) for engine_id in available}

        budgets = {}
        # 不同引擎运行特征不同：LLM 走远端 + 沙箱编译，GP/MCTS/RL 是本地 CPU 进化，
        # rule_seed 直接出种子。给慢引擎更宽时间窗，避免 60s 一刀切把 LLM 全砍掉。
        max_time_by_engine = {
            "llm_primary": 180.0,
            "gp_classic": 120.0,
            "mcts_guided": 90.0,
            "rl_alphagen": 90.0,
            "rule_seed": 30.0,
        }
        for engine_id in available:
            weight = weights.get(engine_id, 1.0 / len(available))
            count = max(3, int(total_count * weight))
            budgets[engine_id] = SearchBudget(
                candidate_count=count,
                max_time_sec=max_time_by_engine.get(engine_id, 60.0),
            )

        return budgets

    def _apply_quality_feedback(self, weights: dict[str, float]) -> dict[str, float]:
        adjusted = dict(weights)
        for engine_id in self._quality_feedback:
            stats = self._feedback_totals(engine_id)
            raw = max(1, int(stats.get("raw", 0)))
            accepted_rate = float(stats.get("accepted", 0)) / raw
            validated_rate = float(stats.get("validated", 0)) / raw
            if engine_id in {"mcts_guided", "rl_alphagen"} and accepted_rate <= 0.0 and raw >= 10:
                adjusted[engine_id] = adjusted.get(engine_id, 0.0) * 0.5
            elif accepted_rate >= 0.10 or validated_rate >= 0.20:
                adjusted[engine_id] = adjusted.get(engine_id, 0.0) * 1.2
        total = sum(max(0.0, value) for value in adjusted.values())
        if total <= 0:
            return weights
        return {key: max(0.0, value) / total for key, value in adjusted.items()}

    def record_quality_feedback(
        self,
        raw: list[FactorCandidate],
        validated: list[FactorCandidate],
        admitted: list[dict[str, Any]],
    ) -> None:
        run_stats: dict[str, dict[str, int]] = {}
        for candidate in raw or []:
            engine = str(getattr(candidate, "generation_engine", "") or "unknown")
            bucket = run_stats.setdefault(
                engine, {"raw": 0, "validated": 0, "accepted": 0, "promoted": 0}
            )
            bucket["raw"] += 1
        for candidate in validated or []:
            engine = str(getattr(candidate, "generation_engine", "") or "unknown")
            bucket = run_stats.setdefault(
                engine, {"raw": 0, "validated": 0, "accepted": 0, "promoted": 0}
            )
            bucket["validated"] += 1
        for item in admitted or []:
            record = dict((item or {}).get("record") or {})
            engine = str(record.get("generation_engine") or "unknown")
            bucket = run_stats.setdefault(
                engine, {"raw": 0, "validated": 0, "accepted": 0, "promoted": 0}
            )
            bucket["accepted"] += 1
            validation_summary = dict(record.get("validation_summary") or {})
            if validation_summary.get("quality_status") == "promoted":
                bucket["promoted"] += 1
        for engine, stats in run_stats.items():
            self._quality_feedback.setdefault(engine, deque(maxlen=20)).append(stats)

    def _feedback_totals(self, engine_id: str) -> dict[str, int]:
        totals = {"raw": 0, "validated": 0, "accepted": 0, "promoted": 0}
        for stats in self._quality_feedback.get(engine_id, deque()):
            for key in totals:
                totals[key] += int(stats.get(key, 0) or 0)
        return totals

    def status(self) -> dict[str, Any]:
        """所有引擎状态。"""
        engine_status = {
            engine_id: engine.get_status().to_dict()
            for engine_id, engine in self._engines.items()
        }
        return {
            "engines": engine_status,
            "quality_feedback": {
                engine_id: {
                    **self._feedback_totals(engine_id),
                    "window_size": len(history),
                }
                for engine_id, history in self._quality_feedback.items()
            },
        }
