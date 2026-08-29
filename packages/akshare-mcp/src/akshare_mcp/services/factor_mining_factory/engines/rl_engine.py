"""强化学习搜索引擎 — 将因子生成建模为序列决策问题。

参考：AlphaGen (GitHub RL-MLDM) — PPO 序列决策
      AlphaAgentEvo (ICLR 2026) — 自进化 Agentic RL
      Synergistic Alpha (arXiv:2401.02710) — RL 扩展搜索空间

核心思想：
- State: 当前部分表达式 + 市场特征摘要
- Action: 选择下一个 token（算子/字段/常数/终止）
- Reward: IC + 新颖性奖励 - 复杂度惩罚
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any

import numpy as np

from .base import EngineStatus, FactorCandidate, SearchBudget

logger = logging.getLogger(__name__)

# Token 词汇表
_FIELD_TOKENS = [
    "open", "high", "low", "close", "volume", "amount",
    "returns_1d", "return_5d", "return_20d",
    "momentum_20d", "momentum_60d", "volatility_20d", "volume_ratio_5_20",
]
_OP_TOKENS = ["ts_mean", "ts_std", "ts_rank", "zscore", "delta", "delay",
              "abs", "sign", "log1p", "rank"]
_BINARY_TOKENS = ["+", "-", "*", "/"]
_WINDOW_TOKENS = ["5", "10", "20", "60"]
_SPECIAL_TOKENS = ["(", ")", ",", "END"]

ALL_TOKENS = _FIELD_TOKENS + _OP_TOKENS + _BINARY_TOKENS + _WINDOW_TOKENS + _SPECIAL_TOKENS

# 表达式模板（用于 policy 初始化）
_EXPRESSION_TEMPLATES = [
    "{op}({field}, {window})",
    "({field1} {bin} {field2})",
    "{op}(({field1} {bin} {field2}), {window})",
    "{unary}({field})",
    "{op}({field1}, {window}) {bin} {op2}({field2}, {window2})",
    "{op}(rank({field1}), {window}) {bin} {op2}(({field2} {bin2} {field3}), {window2})",
    "zscore(({field1} {bin} {field2}), {window}) {bin2} ts_rank({field3}, {window2})",
]


class RLPolicy:
    """简化版 RL Policy — 基于 epsilon-greedy + 经验回放。

    完整版应使用 PPO/GFlowNet，此处为可运行的简化实现。
    后续可替换为 PyTorch 训练的神经网络 policy。
    """

    def __init__(self, epsilon: float = 0.3, learning_rate: float = 0.01):
        self.epsilon = epsilon
        self.learning_rate = learning_rate
        # Q-table: (state_hash, action) → value
        self._q_table: dict[tuple[str, str], float] = {}
        self._experience: list[dict[str, Any]] = []

    def select_action(self, state: list[str], available_actions: list[str]) -> str:
        """Epsilon-greedy 动作选择。"""
        if not available_actions:
            return "END"

        if random.random() < self.epsilon:
            return random.choice(available_actions)

        # Greedy
        state_hash = self._hash_state(state)
        best_action = available_actions[0]
        best_value = -float("inf")
        for action in available_actions:
            value = self._q_table.get((state_hash, action), 0.0)
            if value > best_value:
                best_value = value
                best_action = action
        return best_action

    def update(self, state: list[str], action: str, reward: float):
        """更新 Q 值。"""
        state_hash = self._hash_state(state)
        key = (state_hash, action)
        old_value = self._q_table.get(key, 0.0)
        self._q_table[key] = old_value + self.learning_rate * (reward - old_value)

        self._experience.append({"state": state_hash, "action": action, "reward": reward})
        # 限制经验大小
        if len(self._experience) > 5000:
            self._experience = self._experience[-3000:]

    @staticmethod
    def _hash_state(state: list[str]) -> str:
        return "|".join(state[-5:])  # 只看最近 5 个 token


class RLAlphaGenEngine:
    """强化学习因子生成引擎。

    使用 RL policy 逐 token 生成因子表达式，
    通过编译验证和 IC 反馈更新 policy。
    """

    engine_id: str
    engine_type: str = "rl"

    def __init__(
        self,
        engine_id: str = "rl_alphagen",
        max_tokens: int = 12,
        episodes_per_run: int = 100,
        epsilon: float = 0.3,
    ):
        self.engine_id = engine_id
        self.max_tokens = max_tokens
        self.episodes_per_run = episodes_per_run
        self._policy = RLPolicy(epsilon=epsilon)
        self._success_count = 0
        self._failure_count = 0
        self._last_run_at: str | None = None
        self._last_error: str | None = None
        self._total_episodes = 0

    async def generate(
        self,
        context: Any,
        budget: SearchBudget,
    ) -> list[FactorCandidate]:
        """RL 生成因子。"""
        try:
            candidates = []
            candidates.extend(
                await self._from_blueprints(context, max(1, budget.candidate_count // 3))
            )
            episodes = min(self.episodes_per_run, budget.max_iterations)

            for episode in range(episodes):
                expression, trajectory = self._rollout()

                if expression and self._validate(expression):
                    reward = await self._compute_reward(expression, context)
                    # 更新 policy
                    for state, action in trajectory:
                        self._policy.update(state, action, reward)

                    if reward > 0.3:  # 只保留高奖励的
                        candidates.append(self._build_candidate(expression, episode, reward))

                self._total_episodes += 1

            # 去重
            seen = set()
            unique = []
            for c in candidates:
                if c.expression_dsl not in seen:
                    seen.add(c.expression_dsl)
                    unique.append(c)

            self._success_count += 1
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            return unique[:budget.candidate_count]

        except Exception as exc:
            self._failure_count += 1
            self._last_error = str(exc)
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            logger.warning("RLAlphaGenEngine: failed: %s", exc)
            return []

    async def _from_blueprints(self, context: Any, count: int) -> list[FactorCandidate]:
        from ..blueprints import candidate_from_blueprint

        evaluator = getattr(context, "quick_evidence_evaluator", None)
        if evaluator is None:
            return []

        import inspect

        candidates: list[FactorCandidate] = []
        for index, blueprint in enumerate(
            list(getattr(context, "alpha_blueprints", []) or [])[: max(0, count)]
        ):
            candidate = candidate_from_blueprint(
                blueprint,
                engine_id=self.engine_id,
                index=index,
                mode="rl_blueprint_seed",
            )
            try:
                evidence = evaluator(candidate)
                if inspect.isawaitable(evidence):
                    evidence = await evidence
                if isinstance(evidence, dict):
                    candidate.quick_evidence = dict(evidence)
                    if evidence.get("passed"):
                        candidate.fitness = float(evidence.get("quality_score") or 0.0) / 20.0
                        candidates.append(candidate)
            except Exception:
                continue
        return candidates

    def _rollout(self) -> tuple[str | None, list[tuple[list[str], str]]]:
        """执行一次 rollout，生成表达式。"""
        state: list[str] = []
        trajectory: list[tuple[list[str], str]] = []

        # 选择模板类型
        template_type = self._policy.select_action(
            state,
            ["ts_op", "binary", "compound", "deep_compound", "nested_spread"],
        )
        trajectory.append((list(state), template_type))
        state.append(template_type)

        if template_type == "ts_op":
            # ts_op(field, window)
            op = self._policy.select_action(state, _OP_TOKENS[:6])
            trajectory.append((list(state), op))
            state.append(op)

            field = self._policy.select_action(state, _FIELD_TOKENS)
            trajectory.append((list(state), field))
            state.append(field)

            window = self._policy.select_action(state, _WINDOW_TOKENS)
            trajectory.append((list(state), window))
            state.append(window)

            return f"{op}({field}, {window})", trajectory

        elif template_type == "binary":
            # (field1 op field2)
            field1 = self._policy.select_action(state, _FIELD_TOKENS)
            trajectory.append((list(state), field1))
            state.append(field1)

            op = self._policy.select_action(state, _BINARY_TOKENS)
            trajectory.append((list(state), op))
            state.append(op)

            field2 = self._policy.select_action(state, _FIELD_TOKENS)
            trajectory.append((list(state), field2))
            state.append(field2)

            return f"({field1} {op} {field2})", trajectory

        elif template_type == "compound":
            # op1(field1, w1) bin op2(field2, w2)
            op1 = self._policy.select_action(state, _OP_TOKENS[:6])
            state.append(op1)
            field1 = self._policy.select_action(state, _FIELD_TOKENS)
            state.append(field1)
            w1 = self._policy.select_action(state, _WINDOW_TOKENS)
            state.append(w1)
            bin_op = self._policy.select_action(state, _BINARY_TOKENS)
            state.append(bin_op)
            op2 = self._policy.select_action(state, _OP_TOKENS[:6])
            state.append(op2)
            field2 = self._policy.select_action(state, _FIELD_TOKENS)
            state.append(field2)
            w2 = self._policy.select_action(state, _WINDOW_TOKENS)
            state.append(w2)

            trajectory.append((["compound"], f"{op1}_{field1}_{op2}_{field2}"))
            return f"{op1}({field1}, {w1}) {bin_op} {op2}({field2}, {w2})", trajectory

        elif template_type == "deep_compound":
            op1 = self._policy.select_action(state, _OP_TOKENS[:6])
            field1 = self._policy.select_action(state, _FIELD_TOKENS)
            w1 = self._policy.select_action(state, _WINDOW_TOKENS)
            bin_op = self._policy.select_action(state, _BINARY_TOKENS)
            op2 = self._policy.select_action(state, _OP_TOKENS[:6])
            field2 = self._policy.select_action(state, _FIELD_TOKENS)
            bin2 = self._policy.select_action(state, _BINARY_TOKENS)
            field3 = self._policy.select_action(state, _FIELD_TOKENS)
            w2 = self._policy.select_action(state, _WINDOW_TOKENS)
            trajectory.append((["deep_compound"], f"{op1}_{field1}_{op2}_{field2}_{field3}"))
            return (
                f"{op1}(rank({field1}), {w1}) {bin_op} "
                f"{op2}(({field2} {bin2} {field3}), {w2})"
            ), trajectory

        elif template_type == "nested_spread":
            field1 = self._policy.select_action(state, _FIELD_TOKENS)
            bin_op = self._policy.select_action(state, _BINARY_TOKENS)
            field2 = self._policy.select_action(state, _FIELD_TOKENS)
            w1 = self._policy.select_action(state, _WINDOW_TOKENS)
            bin2 = self._policy.select_action(state, _BINARY_TOKENS)
            field3 = self._policy.select_action(state, _FIELD_TOKENS)
            w2 = self._policy.select_action(state, _WINDOW_TOKENS)
            trajectory.append((["nested_spread"], f"{field1}_{field2}_{field3}"))
            return (
                f"zscore(({field1} {bin_op} {field2}), {w1}) {bin2} "
                f"ts_rank({field3}, {w2})"
            ), trajectory

        return None, trajectory

    def _validate(self, expression: str) -> bool:
        """验证表达式。"""
        from ...factor_candidate_compiler import compile_factor_candidate
        try:
            compiled = compile_factor_candidate({
                "name": "rl_probe",
                "hypothesis": "RL probe",
                "family": "custom",
                "inputs": ["close"],
                "expression_dsl": expression,
            })
            return bool(compiled.get("valid"))
        except Exception:
            return False

    async def _compute_reward(self, expression: str, context: Any) -> float:
        """计算奖励。"""
        reward = 0.0

        # 编译有效性
        from ...factor_candidate_compiler import compile_factor_candidate
        try:
            compiled = compile_factor_candidate({
                "name": "rl_reward",
                "hypothesis": "RL reward",
                "family": "custom",
                "inputs": ["close"],
                "expression_dsl": expression,
            })
            if compiled.get("valid"):
                reward += 0.2
                # 复杂度适中
                complexity = compiled.get("complexity", {}).get("score", 0)
                if 10 <= complexity <= 50:
                    reward += 0.1
                # 多字段
                fields = len(compiled.get("referenced_fields", []))
                reward += min(fields * 0.05, 0.15)
                # 新颖性（不在种子中）
                seed_exprs = {s.get("expression_dsl", "") for s in getattr(context, "seed_factors", [])}
                if expression not in seed_exprs:
                    reward += 0.05
        except Exception:
            pass

        evaluator = getattr(context, "quick_ic_evaluator", None)
        if evaluator is None:
            return 0.0

        try:
            candidate = FactorCandidate(
                name="rl_reward_probe",
                hypothesis="RL reward probe",
                economic_hypothesis="RL reward probe requiring quick evidence.",
                family=self._infer_family(expression),
                factor_family=self._infer_family(expression),
                inputs=self._extract_fields(expression),
                expression_dsl=expression,
                generation_engine=self.engine_id,
                risk_exposure_hint={"style": ["rl_search"], "risk": ["overfit"]},
            )
            import inspect

            feedback = evaluator(candidate)
            if inspect.isawaitable(feedback):
                feedback = await feedback
            if isinstance(feedback, dict):
                candidate.quick_evidence = dict(feedback)
                if not feedback.get("passed"):
                    return 0.0
                ic_value = float(feedback.get("rank_ic_mean") or 0.0)
                quality = float(feedback.get("quality_score") or 0.0)
                return reward + min(abs(ic_value) * 8.0, 0.4) + min(quality / 100.0, 0.4)
            ic_value = float(feedback or 0.0)
        except Exception:
            ic_value = 0.0
        if abs(ic_value) <= 0.0:
            return 0.0
        return reward + min(abs(ic_value) * 10.0, 0.5)

    def _build_candidate(self, expression: str, episode: int, reward: float) -> FactorCandidate:
        """构建候选。"""
        return FactorCandidate(
            name=f"rl_factor_{episode + 1}",
            hypothesis=f"RL-discovered: {expression[:60]}",
            economic_hypothesis="RL-discovered expression with positive quick evidence feedback.",
            family=self._infer_family(expression),
            factor_family=self._infer_family(expression),
            inputs=self._extract_fields(expression),
            expression_dsl=expression,
            expected_holding_period=10,
            expected_horizon=10,
            risk_exposure_hint={"style": ["rl_search"], "risk": ["overfit"]},
            complexity_hint="medium",
            novelty_rationale="Discovered via reinforcement learning sequential decision",
            generation_engine=self.engine_id,
            generation_trace={
                "mode": "rl_sequential",
                "episode": episode,
                "reward": round(reward, 4),
                "total_episodes": self._total_episodes,
            },
            fitness=reward,
        )

    @staticmethod
    def _infer_family(expr: str) -> str:
        if "momentum" in expr or "return" in expr:
            return "momentum"
        if "volatility" in expr:
            return "volatility"
        if "volume" in expr:
            return "liquidity"
        return "custom"

    @staticmethod
    def _extract_fields(expr: str) -> list[str]:
        return [f for f in _FIELD_TOKENS if f in expr]

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
