"""MCTS 引导搜索引擎 — 蒙特卡洛树搜索因子发现。

参考：AlphaCFG (arXiv:2601.22119) — 文法引导 MCTS
      LLM-MCTS (arXiv:2505.11122) — 回测反馈引导 + 频繁子树回避
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import datetime, timezone
from typing import Any

from .base import EngineStatus, FactorCandidate, SearchBudget

logger = logging.getLogger(__name__)

# DSL 文法规则（上下文无关文法）
_GRAMMAR = {
    "expr": [
        ("ts_op", 0.35),
        ("binary", 0.30),
        ("unary", 0.20),
        ("terminal", 0.15),
    ],
    "ts_op": ["ts_mean", "ts_std", "ts_rank", "zscore", "delta", "delay"],
    "unary": ["abs", "sign", "log1p", "rank", "-"],
    "binary": ["+", "-", "*", "/"],
    "terminal": [
        "open", "high", "low", "close", "volume", "amount",
        "returns_1d", "return_5d", "return_20d",
        "momentum_20d", "momentum_60d", "volatility_20d", "volume_ratio_5_20",
    ],
    "window": [5, 10, 20, 60],
}


class MCTSNode:
    """MCTS 树节点。"""

    def __init__(self, state: list[str], parent: "MCTSNode | None" = None):
        self.state = state  # 当前部分表达式 token 序列
        self.parent = parent
        self.children: list["MCTSNode"] = []
        self.visits: int = 0
        self.total_reward: float = 0.0
        self.untried_actions: list[str] | None = None

    @property
    def is_terminal(self) -> bool:
        """是否为完整表达式。"""
        return len(self.state) >= 3 and self._is_complete()

    @property
    def ucb1(self) -> float:
        """UCB1 值。"""
        if self.visits == 0:
            return float("inf")
        exploitation = self.total_reward / self.visits
        exploration = math.sqrt(2 * math.log(self.parent.visits + 1) / self.visits) if self.parent else 0
        return exploitation + 1.4 * exploration

    def _is_complete(self) -> bool:
        """检查表达式是否完整（简化判断）。"""
        expr = " ".join(self.state)
        # 简单启发式：有终端符且有算子
        has_terminal = any(t in _GRAMMAR["terminal"] for t in self.state)
        has_op = any(t in _GRAMMAR["ts_op"] + _GRAMMAR["unary"] for t in self.state)
        return has_terminal and (has_op or len(self.state) >= 2)

    def get_available_actions(self) -> list[str]:
        """获取可用动作（基于文法规则）。"""
        if self.untried_actions is not None:
            return self.untried_actions

        depth = len(self.state)
        actions = []

        if depth == 0:
            # 第一步：选择表达式类型
            for expr_type, weight in _GRAMMAR["expr"]:
                actions.append(expr_type)
        elif depth == 1:
            # 第二步：根据类型展开
            expr_type = self.state[0]
            if expr_type == "ts_op":
                actions = list(_GRAMMAR["ts_op"])
            elif expr_type == "unary":
                actions = list(_GRAMMAR["unary"])
            elif expr_type == "binary":
                actions = list(_GRAMMAR["binary"])
            elif expr_type == "terminal":
                actions = list(_GRAMMAR["terminal"])
        elif depth == 2:
            # 第三步：选择操作数
            actions = list(_GRAMMAR["terminal"])
        elif depth == 3:
            # 第四步：选择窗口或第二操作数
            if self.state[0] in ("ts_op",):
                actions = [str(w) for w in _GRAMMAR["window"]]
            else:
                actions = list(_GRAMMAR["terminal"])
        else:
            actions = ["DONE"]

        self.untried_actions = actions
        return actions


def _build_expression_from_state(state: list[str]) -> str:
    """从 MCTS 状态构建 DSL 表达式。"""
    if not state:
        return "close"

    expr_type = state[0] if state else "terminal"

    if expr_type == "ts_op" and len(state) >= 4:
        op = state[1]
        operand = state[2]
        window = state[3]
        return f"{op}({operand}, {window})"
    elif expr_type == "unary" and len(state) >= 3:
        op = state[1]
        operand = state[2]
        if op == "-":
            return f"-{operand}"
        return f"{op}({operand})"
    elif expr_type == "binary" and len(state) >= 4:
        op = state[1]
        left = state[2]
        right = state[3] if len(state) > 3 else "close"
        return f"({left} {op} {right})"
    elif expr_type == "terminal" and len(state) >= 2:
        return state[1]
    elif len(state) >= 2:
        return state[1] if state[1] in _GRAMMAR["terminal"] else "close"

    return "close"


class MCTSSearchEngine:
    """蒙特卡洛树搜索因子发现引擎。

    使用 CFG 定义搜索空间，MCTS 在语法树上探索，
    回测反馈引导搜索方向，频繁子树回避机制防止重复。
    """

    engine_id: str
    engine_type: str = "mcts"

    def __init__(
        self,
        engine_id: str = "mcts_guided",
        simulations: int = 500,
        max_depth: int = 5,
    ):
        self.engine_id = engine_id
        self.simulations = simulations
        self.max_depth = max_depth
        self._success_count = 0
        self._failure_count = 0
        self._last_run_at: str | None = None
        self._last_error: str | None = None
        self._visited_expressions: set[str] = set()  # 频繁子树回避

    async def generate(
        self,
        context: Any,
        budget: SearchBudget,
    ) -> list[FactorCandidate]:
        """MCTS 搜索生成候选因子。"""
        try:
            candidates = self._blueprint_candidates(context, max(1, budget.candidate_count // 2))
            simulations_per_candidate = max(30, self.simulations // max(1, budget.candidate_count))

            for i in range(budget.candidate_count * 3):  # 过采样
                if len(candidates) >= budget.candidate_count:
                    break

                root = MCTSNode(state=[])
                best_expr = self._run_mcts(root, simulations_per_candidate)

                if best_expr and best_expr != "close" and best_expr not in self._visited_expressions:
                    # 验证编译
                    if self._validate_expression(best_expr):
                        self._visited_expressions.add(best_expr)
                        candidates.append(self._build_candidate(best_expr, i))

                # P1-F: 每次 MCTS 搜索后让出 event loop,使并行 llm_primary httpx 回调有调度窗口。
                # _run_mcts 是纯 CPU 同步、整循环无 await,会独占 loop 阻塞 LLM 引擎。
                await asyncio.sleep(0)

            # 如果 MCTS 产出不足，用随机合法表达式补充
            if len(candidates) < budget.candidate_count:
                extra = self._generate_random_valid(budget.candidate_count - len(candidates))
                candidates.extend(extra)

            # 频繁子树回避：限制缓存大小
            if len(self._visited_expressions) > 1000:
                self._visited_expressions = set(list(self._visited_expressions)[-500:])

            self._success_count += 1
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            candidates = await self._rank_with_quick_evidence(candidates, context)
            return candidates[:budget.candidate_count]

        except Exception as exc:
            self._failure_count += 1
            self._last_error = str(exc)
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            logger.warning("MCTSSearchEngine: failed: %s", exc)
            return []

    def _blueprint_candidates(self, context: Any, count: int) -> list[FactorCandidate]:
        from ..blueprints import candidate_from_blueprint

        candidates: list[FactorCandidate] = []
        for index, blueprint in enumerate(
            list(getattr(context, "alpha_blueprints", []) or [])[: max(0, count)]
        ):
            candidates.append(
                candidate_from_blueprint(
                    blueprint,
                    engine_id=self.engine_id,
                    index=index,
                    mode="mcts_blueprint_seed",
                )
            )
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
            return unique

        import inspect

        for candidate in unique:
            try:
                evidence = evaluator(candidate)
                if inspect.isawaitable(evidence):
                    evidence = await evidence
                if isinstance(evidence, dict):
                    candidate.quick_evidence = dict(evidence)
                    candidate.fitness = (
                        float(evidence.get("quality_score") or 0.0) / 20.0
                        if evidence.get("passed")
                        else 0.0
                    )
            except Exception:
                candidate.fitness = 0.0
        unique.sort(
            key=lambda item: (
                bool((getattr(item, "quick_evidence", None) or {}).get("passed")),
                float((getattr(item, "quick_evidence", None) or {}).get("quality_score") or 0.0),
            ),
            reverse=True,
        )
        return unique

    def _generate_random_valid(self, count: int) -> list[FactorCandidate]:
        """生成随机合法表达式作为补充。"""
        candidates = []
        attempts = 0
        while len(candidates) < count and attempts < count * 5:
            attempts += 1
            # 随机构建完整状态
            expr_type = random.choice([t for t, _ in _GRAMMAR["expr"]])
            if expr_type == "ts_op":
                op = random.choice(_GRAMMAR["ts_op"])
                field = random.choice(_GRAMMAR["terminal"])
                window = str(random.choice(_GRAMMAR["window"]))
                expr = f"{op}({field}, {window})"
            elif expr_type == "unary":
                op = random.choice(_GRAMMAR["unary"])
                field = random.choice(_GRAMMAR["terminal"])
                expr = f"-{field}" if op == "-" else f"{op}({field})"
            elif expr_type == "binary":
                op = random.choice(_GRAMMAR["binary"])
                left = random.choice(_GRAMMAR["terminal"])
                right = random.choice(_GRAMMAR["terminal"])
                if left == right:
                    continue
                expr = f"({left} {op} {right})"
            else:
                expr = random.choice(_GRAMMAR["terminal"])

            if expr not in self._visited_expressions and self._validate_expression(expr):
                self._visited_expressions.add(expr)
                candidates.append(self._build_candidate(expr, len(candidates)))

        return candidates

    def _run_mcts(self, root: MCTSNode, simulations: int) -> str | None:
        """运行 MCTS 搜索。"""
        for _ in range(simulations):
            # Selection
            node = self._select(root)

            # Expansion
            if not node.is_terminal:
                node = self._expand(node)

            # Simulation
            reward = self._simulate(node)

            # Backpropagation
            self._backpropagate(node, reward)

        # 提取最佳路径
        return self._extract_best(root)

    def _select(self, node: MCTSNode) -> MCTSNode:
        """UCB1 选择。"""
        while node.children and node.is_terminal is False:
            # 如果还有未尝试的动作，返回当前节点
            actions = node.get_available_actions()
            tried = {tuple(child.state) for child in node.children}
            untried = [a for a in actions if tuple(node.state + [a]) not in tried]
            if untried:
                return node
            # 选择 UCB1 最高的子节点
            node = max(node.children, key=lambda n: n.ucb1)
        return node

    def _expand(self, node: MCTSNode) -> MCTSNode:
        """展开一个新子节点。"""
        actions = node.get_available_actions()
        tried = {tuple(child.state) for child in node.children}
        untried = [a for a in actions if tuple(node.state + [a]) not in tried]

        if not untried:
            return node

        action = random.choice(untried)
        child_state = node.state + [action]
        child = MCTSNode(state=child_state, parent=node)
        node.children.append(child)
        return child

    def _simulate(self, node: MCTSNode) -> float:
        """模拟（随机 rollout + 编译验证）。"""
        state = list(node.state)

        # 随机完成表达式
        while len(state) < 4 and (not state or state[-1] != "DONE"):
            depth = len(state)
            if depth == 0:
                choices = [t for t, _ in _GRAMMAR["expr"]]
                state.append(random.choice(choices))
            elif depth == 1:
                expr_type = state[0]
                pool = _GRAMMAR.get(expr_type, _GRAMMAR["terminal"])
                state.append(random.choice(pool))
            elif depth == 2:
                state.append(random.choice(_GRAMMAR["terminal"]))
            elif depth == 3:
                if state[0] == "ts_op":
                    state.append(str(random.choice(_GRAMMAR["window"])))
                else:
                    state.append(random.choice(_GRAMMAR["terminal"]))
            else:
                break

        # 构建表达式并评估
        expr = _build_expression_from_state(state)

        # 奖励：编译有效性 + 新颖性
        reward = 0.0
        if self._validate_expression(expr):
            reward += 0.6
            # 新颖性奖励
            if expr not in self._visited_expressions:
                reward += 0.3
            # 复杂度适中奖励
            if len(expr) > 10 and len(expr) < 80:
                reward += 0.1
        return reward

    def _backpropagate(self, node: MCTSNode, reward: float):
        """反向传播。"""
        while node is not None:
            node.visits += 1
            node.total_reward += reward
            node = node.parent

    def _extract_best(self, root: MCTSNode) -> str | None:
        """提取最佳表达式。"""
        if not root.children:
            return None

        # 选择访问次数最多的路径
        best_child = max(root.children, key=lambda n: n.visits)
        state = best_child.state

        # 继续沿最佳路径
        node = best_child
        while node.children:
            node = max(node.children, key=lambda n: n.visits)
            state = node.state

        expr = _build_expression_from_state(state)
        return expr if expr != "close" else None

    def _validate_expression(self, expr: str) -> bool:
        """验证表达式是否可编译。"""
        from ...factor_candidate_compiler import compile_factor_candidate
        try:
            compiled = compile_factor_candidate({
                "name": "mcts_probe",
                "hypothesis": "MCTS probe",
                "family": "custom",
                "inputs": ["close"],
                "expression_dsl": expr,
            })
            return bool(compiled.get("valid"))
        except Exception:
            return False

    def _build_candidate(self, expr: str, index: int) -> FactorCandidate:
        """构建候选因子。"""
        from ...factor_candidate_compiler import compile_factor_candidate

        try:
            compiled = compile_factor_candidate({
                "name": f"mcts_factor_{index + 1}",
                "hypothesis": "MCTS-discovered factor",
                "family": "custom",
                "inputs": ["close"],
                "expression_dsl": expr,
            })
            inputs = compiled.get("referenced_fields", ["close"])
        except Exception:
            inputs = ["close"]

        return FactorCandidate(
            name=f"mcts_factor_{index + 1}",
            hypothesis=f"MCTS-discovered: {expr[:60]}",
            economic_hypothesis="Grammar-search expression requiring quick and strict IC evidence.",
            family=self._infer_family(expr),
            factor_family=self._infer_family(expr),
            inputs=inputs,
            expression_dsl=expr,
            expected_holding_period=10,
            expected_horizon=10,
            risk_exposure_hint={"style": ["grammar_search"], "risk": ["overfit"]},
            complexity_hint="medium",
            novelty_rationale="Discovered via Monte Carlo Tree Search with grammar guidance",
            generation_engine=self.engine_id,
            generation_trace={
                "mode": "mcts_grammar_guided",
                "simulations": self.simulations,
            },
        )

    @staticmethod
    def _infer_family(expr: str) -> str:
        """从表达式推断家族。"""
        expr_lower = expr.lower()
        if "momentum" in expr_lower or "return" in expr_lower:
            return "momentum"
        if "volatility" in expr_lower:
            return "volatility"
        if "volume" in expr_lower:
            return "liquidity"
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
