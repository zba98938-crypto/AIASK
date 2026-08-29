"""SQLite 适配器 — 策略超市 Mixin (thin entry-point, combines 5 sub-mixins)"""

from .strategy_crud import StrategyCrudMixin
from .strategy_incubation import StrategyIncubationMixin
from .strategy_runtime import StrategyRuntimeMixin
from .strategy_vector import StrategyVectorMixin
from .strategy_ai import StrategyAIMixin
from .strategy_trade_prediction import StrategyTradePredictionMixin


class StrategyMixin(
    StrategyCrudMixin,
    StrategyIncubationMixin,
    StrategyRuntimeMixin,
    StrategyVectorMixin,
    StrategyAIMixin,
    StrategyTradePredictionMixin,
):
    """策略超市 — 完整 Mixin（CRUD + 孵化 + 运行时 + 向量 + AI）"""
    pass
