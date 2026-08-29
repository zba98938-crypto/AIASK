"""进化优化器 — 对候选因子做多代进化优化。

参考：AlphaAgent (arXiv:2502.16789) 抗衰减正则化
      CogAlpha (arXiv:2511.18850) 多Agent代码进化
      QuantaAlpha (arXiv:2602.07085) 轨迹级变异/交叉
"""

from __future__ import annotations

import logging
import random
from typing import Any

from ..engines.base import FactorCandidate

logger = logging.getLogger(__name__)


class EvolutionaryOptimizer:
    """因子进化优化器。

    核心机制：
    1. 新颖性压力 (Novelty Pressure): 与活跃池的 DSL 文本相似度惩罚
    2. 复杂度控制 (Complexity Control): 过复杂的表达式降权
    3. 多样性维护 (Diversity): 家族分布均衡
    4. 适应度排序 (Fitness): 编译有效性 + 字段多样性
    """

    def __init__(
        self,
        mutation_rate: float = 0.3,
        novelty_threshold: float = 0.85,
        max_complexity: int = 80,
    ):
        self.mutation_rate = mutation_rate
        self.novelty_threshold = novelty_threshold
        self.max_complexity = max_complexity

    async def evolve(
        self,
        candidates: list[FactorCandidate],
        context: Any,
        generations: int = 5,
        ic_evaluator: Any | None = None,
    ) -> list[FactorCandidate]:
        """对候选因子做多代进化。"""
        if not candidates:
            return []

        population = list(candidates)

        for gen in range(generations):
            # 1. 适应度评估
            await self._evaluate_fitness(population, ic_evaluator=ic_evaluator)

            # 2. 新颖性过滤
            population = self._novelty_filter(population, context)

            # 3. 变异
            mutants = self._mutate_population(population)

            # 4. 合并 + 选择
            combined = population + mutants
            population = self._select_top(combined, size=len(candidates))

        return population

    async def _evaluate_fitness(
        self,
        population: list[FactorCandidate],
        *,
        ic_evaluator: Any | None = None,
    ) -> None:
        """评估适应度（基于编译有效性、结构特征与 IC 反馈）。

        重要：始终重新计算 structural_score，避免上一代 blended fitness 被
        当成 structural 再次混入，造成 fitness 累积放大、把 ``close`` 这类
        最简表达式推到顶部。
        """
        for candidate in population:
            structural_score = self._structural_fitness(candidate)
            candidate.fitness = structural_score
            if ic_evaluator is None:
                continue

            ic_value = await self._safe_ic_evaluate(ic_evaluator, candidate)
            ic_score = abs(ic_value) * 25.0
            # IC 主导（0.4 structural + 0.6 IC）：让真正有预测力的因子上位，
            # 即使 structural 较低（比如简单 momentum）也能因 IC 高被选中；
            # 反之 ``close`` 这种 IC≈0 的表达式会被压到底部。
            candidate.fitness = max(0.0, 0.7 * structural_score + 0.3 * ic_score)
            candidate.generation_trace = {
                **dict(candidate.generation_trace or {}),
                "evolution_structural_score": round(structural_score, 6),
                "evolution_ic_value": round(ic_value, 6),
                "evolution_ic_score": round(ic_score, 6),
                "evolution_fitness_blend": "0.7_structural_0.3_abs_ic_x25",
            }

    def _structural_fitness(self, candidate: FactorCandidate) -> float:
        from ...factor_candidate_compiler import compile_factor_candidate

        try:
            compiled = compile_factor_candidate(candidate.to_validation_dict())
            if not compiled.get("valid"):
                return 0.0
            complexity = compiled.get("complexity", {}).get("score", 0)
            fields = list(compiled.get("referenced_fields", []))
            functions = list(compiled.get("function_calls", []))
            # 最简表达式（``close``、``ts_mean(open,10)``）直接归零，避免被
            # IC 反馈推上前列；要求至少有一个时序算子或两个字段。
            if complexity < 8 or len(functions) == 0:
                return 0.0
            score = 1.0
            if 15 <= complexity <= 50:
                score += 0.5
            elif complexity > self.max_complexity:
                score -= 0.5
            score += min(len(fields) * 0.15, 0.5)
            score += min(len(functions) * 0.10, 0.4)
            return max(0.0, score)
        except Exception:
            return 0.0

    @staticmethod
    async def _safe_ic_evaluate(ic_evaluator: Any, candidate: FactorCandidate) -> float:
        try:
            value = ic_evaluator(candidate)
            import inspect

            if inspect.isawaitable(value):
                value = await value
            return float(value or 0.0)
        except Exception as exc:
            logger.debug("EvolutionaryOptimizer: IC evaluator failed for %s: %s", candidate.name, exc)
            return 0.0

    def _novelty_filter(self, population: list[FactorCandidate], context: Any) -> list[FactorCandidate]:
        """新颖性过滤：与活跃池中因子的文本相似度 < threshold 才保留。"""
        # 获取活跃池的表达式集合
        pool_expressions = set()
        if hasattr(context, "seed_factors"):
            for seed in context.seed_factors:
                expr = seed.get("expression_dsl", "")
                if expr:
                    pool_expressions.add(expr.lower().strip())

        filtered = []
        for candidate in population:
            expr = candidate.expression_dsl.lower().strip()
            # 简单文本相似度检查
            is_novel = True
            for pool_expr in pool_expressions:
                similarity = self._text_similarity(expr, pool_expr)
                if similarity > self.novelty_threshold:
                    is_novel = False
                    break
            if is_novel:
                filtered.append(candidate)

        # 如果过滤太多，保留 top fitness
        if len(filtered) < len(population) * 0.5:
            remaining = [c for c in population if c not in filtered]
            remaining.sort(key=lambda x: -x.fitness)
            fallback_count = max(1, len(population) // 4) if population else 0
            filtered.extend(remaining[:fallback_count])

        return filtered

    def _mutate_population(self, population: list[FactorCandidate]) -> list[FactorCandidate]:
        """对种群做变异操作。"""
        mutants = []
        for candidate in population:
            if random.random() < self.mutation_rate:
                mutant = self._mutate_candidate(candidate)
                if mutant:
                    mutants.append(mutant)
        return mutants

    def _mutate_candidate(self, candidate: FactorCandidate) -> FactorCandidate | None:
        """单个候选的变异。"""
        expr = candidate.expression_dsl
        if not expr:
            return None

        mutation_type = random.choice(["window_perturb", "operator_swap", "field_swap"])

        if mutation_type == "window_perturb":
            # 微调窗口参数
            import re
            windows = re.findall(r"\b(\d+)\b", expr)
            if windows:
                old_w = random.choice(windows)
                new_w = str(max(5, int(old_w) + random.choice([-5, -10, 5, 10, 15])))
                new_expr = expr.replace(old_w, new_w, 1)
            else:
                return None
        elif mutation_type == "operator_swap":
            # 替换算子
            swaps = [("ts_mean", "ts_std"), ("zscore", "ts_rank"), ("delta", "delay")]
            new_expr = expr
            for a, b in swaps:
                if a in expr:
                    new_expr = expr.replace(a, b, 1)
                    break
            if new_expr == expr:
                return None
        elif mutation_type == "field_swap":
            # 替换字段
            fields = ["momentum_20d", "momentum_60d", "volatility_20d", "return_5d", "volume_ratio_5_20"]
            new_expr = expr
            for f in fields:
                if f in expr:
                    replacement = random.choice([x for x in fields if x != f])
                    new_expr = expr.replace(f, replacement, 1)
                    break
            if new_expr == expr:
                return None
        else:
            return None

        return FactorCandidate(
            name=f"{candidate.name}_mut",
            hypothesis=f"Mutated from {candidate.name} via {mutation_type}",
            economic_hypothesis=getattr(candidate, "economic_hypothesis", "") or candidate.hypothesis,
            family=candidate.family,
            factor_family=getattr(candidate, "factor_family", "") or candidate.family,
            inputs=candidate.inputs,
            expression_dsl=new_expr,
            expected_holding_period=candidate.expected_holding_period,
            expected_horizon=getattr(candidate, "expected_horizon", candidate.expected_holding_period),
            complexity_hint=candidate.complexity_hint,
            novelty_rationale=f"Mutation({mutation_type}) of {candidate.name}",
            generation_engine=candidate.generation_engine,
            blueprint_id=getattr(candidate, "blueprint_id", ""),
            risk_exposure_hint=dict(getattr(candidate, "risk_exposure_hint", None) or {}),
            generation_trace={
                "mode": "evolution_mutation",
                "mutation_type": mutation_type,
                "parent": candidate.name,
                "blueprint_id": getattr(candidate, "blueprint_id", ""),
            },
        )

    def _select_top(self, population: list[FactorCandidate], size: int) -> list[FactorCandidate]:
        """选择 top-N（按 fitness 排序）。"""
        # 去重
        seen = set()
        unique = []
        for c in population:
            key = c.expression_dsl.strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(c)

        unique.sort(key=lambda x: -x.fitness)
        return unique[:size]

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """简单文本相似度（Jaccard on tokens）。"""
        import re
        tokens_a = set(re.findall(r"[a-zA-Z_]\w*|\d+", a))
        tokens_b = set(re.findall(r"[a-zA-Z_]\w*|\d+", b))
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b)
        return intersection / union if union > 0 else 0.0
