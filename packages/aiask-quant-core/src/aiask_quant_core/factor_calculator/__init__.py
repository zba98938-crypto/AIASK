"""
因子计算器模块

向后兼容：FactorCalculator 类通过 Mixin 组合所有子模块功能
"""
import numpy as np
from typing import List, Dict, Any, Optional
from numba import jit

from .technical import TechnicalFactorsMixin
from .volatility import VolatilityFactorsMixin
from .volume import VolumeFactorsMixin
from .fundamental import FundamentalFactorsMixin
from .analysis import AnalysisFactorsMixin, _check_monotonicity


class FactorCalculator(
    TechnicalFactorsMixin,
    VolatilityFactorsMixin,
    VolumeFactorsMixin,
    FundamentalFactorsMixin,
    AnalysisFactorsMixin,
):
    """因子计算器 - 支持技术因子、基本面因子、风格因子"""
    pass


factor_calculator = FactorCalculator()

__all__ = [
    'FactorCalculator',
    'factor_calculator',
    '_check_monotonicity',
]
