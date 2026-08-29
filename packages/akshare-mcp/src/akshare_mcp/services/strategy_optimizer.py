"""BanditParameterOptimizer — epsilon-greedy RL parameter evolution."""

from __future__ import annotations

import json
import logging
from typing import Any

from .strategy_spec import StrategySpec

logger = logging.getLogger(__name__)


class BanditParameterOptimizer:
    SCALE_CANDIDATES = (0.8, 0.9, 0.95, 1.05, 1.1, 1.2)

    @staticmethod
    def _mutate_numeric(value: Any, scale: float) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            mutated = max(1, int(round(value * scale)))
            return mutated
        if isinstance(value, float):
            return round(value * scale, 6)
        return value

    @staticmethod
    def _scale_key(scale: Any) -> str:
        try:
            return f"{float(scale):.2f}"
        except Exception:
            return str(scale)

    @staticmethod
    def _reward_from_experiment(experiment: dict) -> float:
        evaluation = dict(experiment.get('evaluation') or {})
        committee_review = dict(evaluation.get('committee_review') or {})
        result = dict(experiment.get('result') or {})
        status = str(experiment.get('status') or '').strip().lower()

        reward = 0.0
        if status == 'accepted':
            reward += 1.0
        elif status == 'rejected':
            reward -= 0.4
        elif status == 'generated':
            reward += 0.1

        final_score = committee_review.get('final_score')
        if isinstance(final_score, (int, float)):
            reward += (float(final_score) - 0.5) * 0.8

        if result.get('passed') is True:
            reward += 0.45
        if result.get('duplicate') is True:
            reward -= 0.2

        return round(reward, 6)

    async def _history_summary(self, db, parent_strategy_id: str) -> dict[str, dict[str, float]]:
        rows = await db.list_strategy_generation_experiments(parent_strategy_id=parent_strategy_id, limit=80) if hasattr(db, 'list_strategy_generation_experiments') else []
        history: dict[str, dict[str, float]] = {}
        for row in rows:
            evaluation = dict(row.get('evaluation') or {})
            generation_reason = dict(evaluation.get('generation_reason') or {})
            scale = generation_reason.get('scale')
            if scale is None:
                continue
            key = self._scale_key(scale)
            bucket = history.setdefault(key, {'count': 0.0, 'reward_sum': 0.0, 'reward_avg': 0.0})
            bucket['count'] += 1.0
            bucket['reward_sum'] += self._reward_from_experiment(row)
            bucket['reward_avg'] = round(bucket['reward_sum'] / max(bucket['count'], 1.0), 6)
        return history

    def _rank_scales(self, history: dict[str, dict[str, float]], hit_rate: float, limit: int) -> list[dict[str, float]]:
        total_observations = sum(float(item.get('count') or 0.0) for item in history.values())
        epsilon = 0.35 if total_observations < 3 or hit_rate < 0.45 else 0.18
        ranked: list[dict[str, float]] = []
        for idx, scale in enumerate(self.SCALE_CANDIDATES):
            key = self._scale_key(scale)
            bucket = history.get(key) or {}
            count = float(bucket.get('count') or 0.0)
            reward_avg = float(bucket.get('reward_avg') or 0.0)
            explore_bonus = epsilon / (count + 1.0)
            prior = max(0.0, 0.08 - abs(scale - 1.0) * 0.1)
            score = reward_avg + explore_bonus + prior
            ranked.append({
                'scale': float(scale),
                'count': count,
                'reward_avg': round(reward_avg, 6),
                'explore_bonus': round(explore_bonus, 6),
                'score': round(score, 6),
                'exploration': count == 0 or idx == 0,
            })
        ranked.sort(key=lambda item: (item['score'], -abs(item['scale'] - 1.0)), reverse=True)
        return ranked[: max(1, min(limit, len(ranked)))]

    async def evolve(self, db, parent_strategy: dict, limit: int = 2) -> list[StrategySpec]:
        metrics = await db.get_strategy_metrics(parent_strategy['id'])
        stats = await db.get_signal_stats(parent_strategy['id'])
        raw_params = parent_strategy.get('params') or {}
        if isinstance(raw_params, str):
            try:
                raw_params = json.loads(raw_params)
            except Exception:
                raw_params = {}
        base = dict(raw_params or {})
        total_signals = int(stats.get('total_signals') or 0)
        hit_rate = float((stats.get('hit_rate') or {}).get(5, (stats.get('hit_rate') or {}).get('5', 0)) or 0)
        history = await self._history_summary(db, parent_strategy['id'])
        selected_scales = self._rank_scales(history, hit_rate, max(1, min(limit, 4)))
        specs: list[StrategySpec] = []
        for idx, item in enumerate(selected_scales, 1):
            scale = float(item['scale'])
            mutated = {
                key: self._mutate_numeric(value, scale)
                if isinstance(value, (int, float)) else value
                for key, value in base.items()
            }
            specs.append(StrategySpec(
                strategy_type=parent_strategy.get('strategy_type') or 'momentum',
                params=mutated,
                name=f"RL 进化 {parent_strategy.get('name') or parent_strategy['id']} #{idx}",
                description='基于历史实验奖励反馈进行参数扰动与探索。',
                tags=['rl_evolved'],
                metadata={
                    'generator_type': 'rl_bandit',
                    'optimizer_type': 'epsilon_greedy_feedback',
                    'generation_reason': {
                        'parent_strategy_id': parent_strategy['id'],
                        'scale': scale,
                        'total_signals': total_signals,
                        'hit_rate_5d': hit_rate,
                        'metrics': metrics,
                        'bandit_feedback': {
                            'historical_count': int(item['count']),
                            'historical_reward_avg': item['reward_avg'],
                            'explore_bonus': item['explore_bonus'],
                            'selection_score': item['score'],
                            'exploration': bool(item['exploration']),
                            'known_scales': history,
                        },
                    },
                    'parent_strategy_id': parent_strategy['id'],
                },
            ))
        return specs
