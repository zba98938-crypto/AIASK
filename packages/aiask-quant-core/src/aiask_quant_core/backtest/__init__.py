"""
回测引擎包 - 使用Numba JIT优化 + Ray并行计算

向后兼容：所有原始导出保持不变
"""

from .utils import _ensure_dict_list, _resolve_slippage_model, _compute_slippage_rate
from .strategies import (
    _backtest_ma_cross_jit,
    _backtest_ma_cross_with_trades_jit,
    _backtest_momentum_jit,
    _backtest_rsi_jit,
)
from .engine import BacktestEngine, backtest_engine
from .advanced import AdvancedBacktestEngine, advanced_backtest_engine
from .strategy_base import IStrategy
from .strategy_registry import StrategyRegistry
from .builtin_strategies import (
    MaCrossStrategy, MomentumStrategy, RsiStrategy, BuyAndHoldStrategy,
)
from .dsl_strategy import DslRuleStrategy
from .single_factor_strategy import (
    ValueFactorStrategy, QualityFactorStrategy, GrowthFactorStrategy,
)
from .multi_factor_strategy import MultiFactorStrategy
from .macro_timing_strategy import MacroTimingStrategy
from .expanded_factory_strategies import (
    VolatilityBreakoutStrategy,
    EventStructureBreakoutStrategy,
    GapFillStrategy,
    MeanReversionShortStrategy,
    SectorRotationStrategy,
    NorthCapitalTrackStrategy,
    MarginDivergenceStrategy,
)

# 自动注册内置策略
for _s in [MaCrossStrategy, MomentumStrategy, RsiStrategy, BuyAndHoldStrategy, DslRuleStrategy]:
    StrategyRegistry.register(_s)

# 注册工厂策略
for _s in [ValueFactorStrategy, QualityFactorStrategy, GrowthFactorStrategy,
           MultiFactorStrategy, MacroTimingStrategy]:
    StrategyRegistry.register(_s)

# PR-B1: 注册真实基本面多因子策略
from .fundamental_factor_strategy import FundamentalMultiFactorStrategy
StrategyRegistry.register(FundamentalMultiFactorStrategy)

for _s in [
    VolatilityBreakoutStrategy,
    EventStructureBreakoutStrategy,
    GapFillStrategy,
    MeanReversionShortStrategy,
    SectorRotationStrategy,
    NorthCapitalTrackStrategy,
    MarginDivergenceStrategy,
]:
    StrategyRegistry.register(_s)

# 条件导入并行引擎
from .parallel import RAY_AVAILABLE
if RAY_AVAILABLE:
    from .parallel import ParallelBacktestEngine

__all__ = [
    # 工具函数
    '_ensure_dict_list', '_resolve_slippage_model', '_compute_slippage_rate',
    # 策略函数
    '_backtest_ma_cross_jit', '_backtest_ma_cross_with_trades_jit',
    '_backtest_momentum_jit', '_backtest_rsi_jit',
    # 引擎类和实例
    'BacktestEngine', 'backtest_engine',
    'AdvancedBacktestEngine', 'advanced_backtest_engine',
    'RAY_AVAILABLE',
    # P5: 策略接口与注册表
    'IStrategy', 'StrategyRegistry',
    'MaCrossStrategy', 'MomentumStrategy', 'RsiStrategy', 'BuyAndHoldStrategy', 'DslRuleStrategy',
    'ValueFactorStrategy', 'QualityFactorStrategy', 'GrowthFactorStrategy',
    'MultiFactorStrategy', 'MacroTimingStrategy',
    'VolatilityBreakoutStrategy', 'EventStructureBreakoutStrategy', 'GapFillStrategy', 'MeanReversionShortStrategy',
    'SectorRotationStrategy', 'NorthCapitalTrackStrategy', 'MarginDivergenceStrategy',
]
