"""遗传规划搜索引擎 — 表达式树进化搜索。

参考：AlphaGen (GitHub), AlphaForge (AAAI 2025), QuantaAlpha (arXiv:2602.07085)
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any

import numpy as np

from .base import EngineStatus, FactorCandidate, SearchBudget

logger = logging.getLogger(__name__)

# DSL 终端符（字段）
_TERMINALS = [
    "open", "high", "low", "close", "volume", "amount",
    "returns_1d", "return_5d", "return_20d",
    "momentum_20d", "momentum_60d", "volatility_20d", "volume_ratio_5_20",
]

# DSL 函数（算子）
_UNARY_OPS = ["abs", "sign", "log1p", "rank", "-"]
_BINARY_OPS = ["+", "-", "*", "/"]
_TS_OPS = [
    ("ts_mean", 2), ("ts_std", 2), ("ts_rank", 2), ("zscore", 2),
    ("delta", 2), ("delay", 2),
]

# 常用窗口参数
_WINDOWS = [5, 10, 20, 60]


class ExpressionTree:
    """表达式树节点。"""

    def __init__(self, node_type: str, value: str = "", children: list | None = None):
        self.node_type = node_type  # "terminal", "unary", "binary", "ts_op"
        self.value = value
        self.children = children or []

    def to_dsl(self) -> str:
        """转换为 DSL 字符串。"""
        if self.node_type == "terminal":
            return self.value
        elif self.node_type == "constant":
            return self.value
        elif self.node_type == "unary":
            child_dsl = self.children[0].to_dsl() if self.children else "close"
            if self.value == "-":
                return f"-{child_dsl}"
            return f"{self.value}({child_dsl})"
        elif self.node_type == "binary":
            left = self.children[0].to_dsl() if len(self.children) > 0 else "close"
            right = self.children[1].to_dsl() if len(self.children) > 1 else "close"
            return f"({left} {self.value} {right})"
        elif self.node_type == "ts_op":
            child_dsl = self.children[0].to_dsl() if self.children else "close"
            window = self.children[1].to_dsl() if len(self.children) > 1 else "20"
            return f"{self.value}({child_dsl}, {window})"
        return self.value

    def depth(self) -> int:
        if not self.children:
            return 1
        return 1 + max(child.depth() for child in self.children)

    def size(self) -> int:
        if not self.children:
            return 1
        return 1 + sum(child.size() for child in self.children)

    def copy(self) -> "ExpressionTree":
        return ExpressionTree(
            node_type=self.node_type,
            value=self.value,
            children=[child.copy() for child in self.children],
        )


def _random_tree(max_depth: int = 4, current_depth: int = 0) -> ExpressionTree:
    """生成随机表达式树。"""
    if current_depth >= max_depth or (current_depth > 1 and random.random() < 0.3):
        # 终端节点
        return ExpressionTree("terminal", random.choice(_TERMINALS))

    choice = random.random()
    if choice < 0.3:
        # 一元算子
        op = random.choice(_UNARY_OPS)
        child = _random_tree(max_depth, current_depth + 1)
        return ExpressionTree("unary", op, [child])
    elif choice < 0.6:
        # 二元算子
        op = random.choice(_BINARY_OPS)
        left = _random_tree(max_depth, current_depth + 1)
        right = _random_tree(max_depth, current_depth + 1)
        return ExpressionTree("binary", op, [left, right])
    else:
        # 时序算子
        ts_op, arity = random.choice(_TS_OPS)
        child = _random_tree(max_depth, current_depth + 1)
        window = ExpressionTree("constant", str(random.choice(_WINDOWS)))
        return ExpressionTree("ts_op", ts_op, [child, window])


def _mutate_tree(tree: ExpressionTree, rate: float = 0.3) -> ExpressionTree:
    """变异操作。"""
    tree = tree.copy()
    if random.random() < rate:
        # 点变异：替换当前节点
        if tree.node_type == "terminal":
            tree.value = random.choice(_TERMINALS)
        elif tree.node_type == "unary":
            tree.value = random.choice(_UNARY_OPS)
        elif tree.node_type == "binary":
            tree.value = random.choice(_BINARY_OPS)
        elif tree.node_type == "ts_op":
            ts_op, _ = random.choice(_TS_OPS)
            tree.value = ts_op
    else:
        # 子树变异
        if tree.children:
            idx = random.randint(0, len(tree.children) - 1)
            if tree.children[idx].node_type != "constant":
                tree.children[idx] = _random_tree(max_depth=3)

    return tree


def _crossover_trees(parent1: ExpressionTree, parent2: ExpressionTree) -> ExpressionTree:
    """交叉操作：从 parent2 取一个子树替换 parent1 的一个子树。"""
    child = parent1.copy()
    if child.children and parent2.children:
        idx = random.randint(0, len(child.children) - 1)
        donor_idx = random.randint(0, len(parent2.children) - 1)
        if child.children[idx].node_type != "constant":
            child.children[idx] = parent2.children[donor_idx].copy()
    return child


class GeneticProgrammingEngine:
    """遗传规划因子搜索引擎。

    将因子表达式表示为表达式树，通过变异/交叉/选择进化。
    """

    engine_id: str
    engine_type: str = "gp"

    def __init__(
        self,
        engine_id: str = "gp_classic",
        population_size: int = 100,
        max_generations: int = 30,
        tournament_size: int = 5,
        mutation_rate: float = 0.3,
        crossover_rate: float = 0.5,
        max_depth: int = 5,
    ):
        self.engine_id = engine_id
        self.population_size = population_size
        self.max_generations = max_generations
        self.tournament_size = tournament_size
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.max_depth = max_depth
        self._success_count = 0
        self._failure_count = 0
        self._last_run_at: str | None = None
        self._last_error: str | None = None

    async def generate(
        self,
        context: Any,
        budget: SearchBudget,
    ) -> list[FactorCandidate]:
        """遗传规划搜索。"""
        try:
            # 1. 初始化种群
            population = self._initialize_population(context)

            # 2. 进化循环
            generations = min(self.max_generations, budget.max_iterations)
            for gen in range(generations):
                # 适应度评估（基于编译有效性 + 复杂度）
                fitness = self._evaluate_fitness(population)

                # 选择
                parents = self._tournament_select(population, fitness)

                # 交叉 + 变异
                offspring = []
                for i in range(0, len(parents) - 1, 2):
                    if random.random() < self.crossover_rate:
                        child = _crossover_trees(parents[i], parents[i + 1])
                    else:
                        child = parents[i].copy()
                    child = _mutate_tree(child, self.mutation_rate)
                    offspring.append(child)

                # 精英保留 + 替换
                elite_count = max(2, self.population_size // 10)
                sorted_pop = sorted(zip(population, fitness), key=lambda x: -x[1])
                elites = [tree for tree, _ in sorted_pop[:elite_count]]
                population = elites + offspring[:self.population_size - elite_count]

                # P1-F: 每代让出 event loop,使并行的 llm_primary httpx 回调有调度窗口。
                # GP 进化主循环纯 CPU 同步、整段无 await,会独占 event loop 阻塞 LLM 引擎。
                await asyncio.sleep(0)

            # 3. 提取 top-K 候选
            final_fitness = self._evaluate_fitness(population)
            candidates = self._extract_top_candidates(population, final_fitness, budget.candidate_count)
            candidates = self._blueprint_candidates(context, budget.candidate_count) + candidates
            candidates = await self._rank_with_quick_evidence(candidates, context)

            self._success_count += 1
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            return candidates[:budget.candidate_count]

        except Exception as exc:
            self._failure_count += 1
            self._last_error = str(exc)
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            logger.warning("GeneticProgrammingEngine: failed: %s", exc)
            return []

    def _blueprint_candidates(self, context: Any, count: int) -> list[FactorCandidate]:
        from ..blueprints import candidate_from_blueprint

        candidates: list[FactorCandidate] = []
        for index, blueprint in enumerate(
            list(getattr(context, "alpha_blueprints", []) or [])[: max(0, count)]
        ):
            candidate = candidate_from_blueprint(
                blueprint,
                engine_id=self.engine_id,
                index=index,
                mode="gp_blueprint_seed",
            )
            candidate.fitness = max(candidate.fitness, 1.0)
            candidates.append(candidate)
        return candidates

    async def _rank_with_quick_evidence(
        self,
        candidates: list[FactorCandidate],
        context: Any,
    ) -> list[FactorCandidate]:
        evaluator = getattr(context, "quick_evidence_evaluator", None)
        unique: list[FactorCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate.expression_dsl or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(candidate)

        if evaluator is None:
            unique.sort(key=lambda item: -float(getattr(item, "fitness", 0.0) or 0.0))
            return unique

        import inspect

        for candidate in unique[: max(1, self.population_size // 5)]:
            try:
                evidence = evaluator(candidate)
                if inspect.isawaitable(evidence):
                    evidence = await evidence
                if isinstance(evidence, dict):
                    candidate.quick_evidence = dict(evidence)
                    if evidence.get("passed"):
                        candidate.fitness = max(
                            float(candidate.fitness or 0.0),
                            float(evidence.get("quality_score") or 0.0) / 20.0,
                        )
                    else:
                        candidate.fitness = min(float(candidate.fitness or 0.0), 0.25)
            except Exception:
                candidate.fitness = 0.0
        unique.sort(
            key=lambda item: (
                bool((getattr(item, "quick_evidence", None) or {}).get("passed")),
                float((getattr(item, "quick_evidence", None) or {}).get("quality_score") or 0.0),
                float(getattr(item, "fitness", 0.0) or 0.0),
            ),
            reverse=True,
        )
        return unique

    def _initialize_population(self, context: Any) -> list[ExpressionTree]:
        """初始化种群：随机树 + 种子因子。"""
        population = []

        # 从种子因子构建初始个体
        seed_factors = getattr(context, "seed_factors", [])
        for seed in seed_factors[:10]:
            expr = seed.get("expression_dsl", "")
            if expr:
                # 简单解析为终端节点（种子因子通常较简单）
                population.append(ExpressionTree("terminal", expr.split("(")[0] if "(" not in expr else "close"))

        # 填充随机个体
        while len(population) < self.population_size:
            population.append(_random_tree(max_depth=self.max_depth))

        return population[:self.population_size]

    def _evaluate_fitness(self, population: list[ExpressionTree]) -> list[float]:
        """适应度评估（快速启发式）。"""
        from ...factor_candidate_compiler import compile_factor_candidate

        fitness_scores = []
        for tree in population:
            dsl = tree.to_dsl()
            score = 0.0

            # 编译有效性
            try:
                compiled = compile_factor_candidate({
                    "name": "gp_candidate",
                    "hypothesis": "GP generated",
                    "family": "custom",
                    "inputs": ["close"],
                    "expression_dsl": dsl,
                })
                if compiled.get("valid"):
                    score += 1.0
                    # 复杂度奖励（适中复杂度最好）
                    complexity = compiled.get("complexity", {}).get("score", 0)
                    if 10 <= complexity <= 50:
                        score += 0.5
                    elif complexity < 10:
                        score += 0.2  # 太简单
                    # 使用多个字段奖励
                    fields = len(compiled.get("referenced_fields", []))
                    score += min(fields * 0.2, 0.6)
                    # 使用时序函数奖励
                    calls = len(compiled.get("function_calls", []))
                    score += min(calls * 0.15, 0.4)
            except Exception:
                score = 0.0

            # 深度惩罚（过深的树容易过拟合）
            depth = tree.depth()
            if depth > 6:
                score *= 0.5

            fitness_scores.append(score)

        return fitness_scores

    def _tournament_select(
        self,
        population: list[ExpressionTree],
        fitness: list[float],
    ) -> list[ExpressionTree]:
        """锦标赛选择。"""
        selected = []
        for _ in range(len(population)):
            tournament = random.sample(range(len(population)), min(self.tournament_size, len(population)))
            winner = max(tournament, key=lambda i: fitness[i])
            selected.append(population[winner].copy())
        return selected

    def _extract_top_candidates(
        self,
        population: list[ExpressionTree],
        fitness: list[float],
        k: int,
    ) -> list[FactorCandidate]:
        """提取 top-K 候选并转换为 FactorCandidate。"""
        from ...factor_candidate_compiler import compile_factor_candidate

        sorted_indices = sorted(range(len(fitness)), key=lambda i: -fitness[i])
        candidates = []
        seen_expressions = set()

        for idx in sorted_indices:
            if len(candidates) >= k:
                break

            tree = population[idx]
            dsl = tree.to_dsl()

            # 去重
            if dsl in seen_expressions:
                continue
            seen_expressions.add(dsl)

            # 验证编译
            try:
                compiled = compile_factor_candidate({
                    "name": f"gp_factor_{len(candidates) + 1}",
                    "hypothesis": "Discovered via genetic programming",
                    "family": "custom",
                    "inputs": compiled.get("referenced_fields", ["close"]) if "compiled" in dir() else ["close"],
                    "expression_dsl": dsl,
                })
                if not compiled.get("valid"):
                    continue

                candidates.append(FactorCandidate(
                    name=f"gp_factor_{len(candidates) + 1}",
                    hypothesis=f"GP-discovered expression: {dsl[:80]}",
                    economic_hypothesis="Data-mined expression requiring quick and strict IC evidence.",
                    family=self._infer_family(compiled.get("referenced_fields", [])),
                    factor_family=self._infer_family(compiled.get("referenced_fields", [])),
                    inputs=compiled.get("referenced_fields", []),
                    expression_dsl=dsl,
                    expected_horizon=10,
                    risk_exposure_hint={"style": ["data_mined"], "risk": ["overfit"]},
                    complexity_hint="medium" if compiled.get("complexity", {}).get("score", 0) < 40 else "high",
                    novelty_rationale="Discovered via genetic programming evolutionary search",
                    generation_engine=self.engine_id,
                    generation_trace={
                        "mode": "genetic_programming",
                        "generation": self.max_generations,
                        "fitness": fitness[idx],
                        "tree_depth": tree.depth(),
                        "tree_size": tree.size(),
                    },
                    fitness=fitness[idx],
                ))
            except Exception:
                continue

        return candidates

    @staticmethod
    def _infer_family(fields: list[str]) -> str:
        """从引用字段推断因子家族。"""
        field_set = set(fields)
        if field_set & {"momentum_20d", "momentum_60d", "return_5d", "return_20d"}:
            return "momentum"
        if field_set & {"volatility_20d"}:
            return "volatility"
        if field_set & {"volume", "volume_ratio_5_20", "amount"}:
            return "liquidity"
        if "returns_1d" in field_set and len(field_set) == 1:
            return "reversal"
        return "custom"

    def get_status(self) -> EngineStatus:
        return EngineStatus(
            engine_id=self.engine_id,
            engine_type=self.engine_type,
            enabled=True,
            ready=True,
            last_run_at=self._last_run_at,
            last_error=self._last_error,
            success_count=self._success_count,
            failure_count=self._failure_count,
        )
