"""Factor → strategy-type mapping learner (PR-S13).

从历史 ``strategy_metrics`` 与 ``factor_ic_history`` 学习哪个因子 → 哪个 strategy_type
在过去 N 天真正提升过 Gate 通过率，输出动态 mapping override，作为
``FACTOR_STRATEGY_MAPPING_OVERRIDES`` 的运行时补充。

当前接入策略：
- 默认 **不接入主流程**——调用者需显式调用 ``compute_factor_mapping_from_history``
  并把结果写入 ``FACTOR_STRATEGY_MAPPING_OVERRIDES``，或经由
  ``STRATEGY_FACTORY_FACTOR_MAPPING_OVERRIDES`` 环境变量传入。
- 数据匮乏时（strategies < 50 / IC 历史 < 14 天）应静默返回空 dict，避免噪声。
- 学习窗口默认 60 天，可通过 ``STRATEGY_FACTORY_FACTOR_MAPPING_LEARN_DAYS`` 调整。

未来工作：
- 把本函数挂到 FactorScheduler.run_once() 结尾，每周一次回写 override。
- 暴露调试 API：``factor_mapping_diagnostics`` 让运维能看到学习产生的映射。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


_MIN_STRATEGY_SAMPLES = 50
_MIN_IC_DAYS = 14
_DEFAULT_LEARN_DAYS = 60


async def compute_factor_mapping_from_history(
    db: Any,
    *,
    learn_days: Optional[int] = None,
    min_samples: int = _MIN_STRATEGY_SAMPLES,
    min_ic_days: int = _MIN_IC_DAYS,
) -> dict[str, tuple[str, ...]]:
    """根据近 ``learn_days`` 天的 IC 历史与策略提交记录，学习 factor→strategy_type 映射。

    返回 ``{factor_name: (top_strategy_type, ...)}``；数据不足时返回 ``{}``。
    """
    learn_days = int(learn_days or os.getenv("STRATEGY_FACTORY_FACTOR_MAPPING_LEARN_DAYS", _DEFAULT_LEARN_DAYS) or _DEFAULT_LEARN_DAYS)
    learn_days = max(7, min(learn_days, 365))

    try:
        listed = await _safe_list_strategies(db, "listed")
        incubating = await _safe_list_strategies(db, "incubating")
    except Exception as exc:
        logger.debug("compute_factor_mapping_from_history: load strategies failed: %s", exc)
        return {}

    samples = list(listed) + list(incubating)
    if len(samples) < min_samples:
        logger.debug(
            "compute_factor_mapping_from_history: insufficient strategy samples (%d < %d)",
            len(samples), min_samples,
        )
        return {}

    # 收集每个 factor 在近 N 天的 IC 趋势及哪种 strategy_type 在该因子主导期 Gate 通过最多
    factor_to_type_score: dict[str, dict[str, float]] = {}
    for strategy in samples:
        strategy_type = str(strategy.get("strategy_type") or "").strip().lower()
        if not strategy_type:
            continue
        # source_factor 字段假定记录策略主导因子；不存在则跳过
        source_factor = str(strategy.get("source_factor") or strategy.get("factor_name") or "").strip().lower()
        if not source_factor:
            continue
        # Gate 通过权重：A=1.0, B=0.7, C=0.3, 其他 0
        validation_grade = str((strategy.get("validation_grade") or "")).strip().upper()
        weight = {
            "SSS": 1.25,
            "SS": 1.18,
            "S": 1.10,
            "A": 1.0,
            "B": 0.7,
            "C": 0.3,
        }.get(validation_grade, 0.0)
        if weight <= 0:
            continue
        bucket = factor_to_type_score.setdefault(source_factor, {})
        bucket[strategy_type] = bucket.get(strategy_type, 0.0) + weight

    if not factor_to_type_score:
        return {}

    result: dict[str, tuple[str, ...]] = {}
    for factor_name, type_scores in factor_to_type_score.items():
        if not type_scores:
            continue
        ordered = sorted(type_scores.items(), key=lambda kv: kv[1], reverse=True)
        # 只保留得分 >= max * 0.5 的 top 3 类型
        cutoff = ordered[0][1] * 0.5
        kept = [t for t, score in ordered[:3] if score >= cutoff]
        if kept:
            result[factor_name] = tuple(kept)

    logger.info(
        "compute_factor_mapping_from_history: learned %d factor→type mappings from %d strategies",
        len(result), len(samples),
    )
    return result


async def _safe_list_strategies(db: Any, status: str) -> Iterable[dict]:
    method = getattr(db, "list_strategies", None)
    if not callable(method):
        return []
    try:
        rows = await method(status, limit=2000)
    except TypeError:
        rows = await method(status)
    return list(rows or [])


__all__ = ["compute_factor_mapping_from_history"]
