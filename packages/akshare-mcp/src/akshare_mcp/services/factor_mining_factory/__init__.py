"""因子挖掘工厂 (Factor Mining Factory)

与策略工厂 (Strategy Factory)、孵化工厂 (Incubation Factory) 并行的第三大核心引擎。
负责因子的自动化搜索、进化优化、验证治理、池管理和反馈闭环。

Architecture:
    Layer 0: DSL & 执行沙箱 (sandbox/)
    Layer 1: 多引擎搜索 (engines/)
    Layer 2: 进化优化 (evolution/)
    Layer 3: 验证治理 (复用现有 factor_validation_pipeline)
    Layer 4: 因子池管理 (pool/)
    Layer 5: 反馈闭环 (feedback/)
"""

from __future__ import annotations

from typing import Optional

_factory_instance: Optional["FactorMiningFactory"] = None


def get_factor_mining_factory() -> "FactorMiningFactory":
    """获取因子挖掘工厂全局单例。"""
    global _factory_instance
    if _factory_instance is None:
        from .factory import FactorMiningFactory
        _factory_instance = FactorMiningFactory()
    return _factory_instance


__all__ = ["get_factor_mining_factory"]
