"""孵化工厂 · 观察生命周期策略（宿主侧兼容再导出）。

决策逻辑已迁入 Strategy Factory（Phase 3）：observe/promote/halt 属于生命周期
**决策**，归 Strategy Factory 所有，而非数据宿主。

本模块保留为再导出薄壳，使 ``from .observation_lifecycle_policy import
ObservationLifecyclePolicy`` 这类既有导入无需改动，并且只经
``strategy_factory.api`` 公共面获取符号。
"""

from __future__ import annotations

from strategy_factory.api.runtime import (
    OBSERVATION_DECISIONS,
    ObservationLifecycleDecision,
    ObservationLifecyclePolicy,
)

__all__ = [
    "OBSERVATION_DECISIONS",
    "ObservationLifecycleDecision",
    "ObservationLifecyclePolicy",
]
