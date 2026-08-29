"""策略抽象基类 — IStrategy ABC"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import numpy as np


StrategySignalEvent = Dict[str, Any]


class IStrategy(ABC):
    """所有策略的抽象基类。

    generate_signals() 返回信号数组: 1=买入, -1=卖出, 0=持有。
    generate_entry_exit_masks() 是便捷方法，将信号转为 entry/exit 掩码供回测引擎使用。
    """

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """策略唯一标识符，如 'ma_cross'"""
        ...

    @classmethod
    @abstractmethod
    def description(cls) -> str:
        """策略描述"""
        ...

    @abstractmethod
    def get_parameters(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    def set_parameters(self, params: Dict[str, Any]) -> None:
        ...

    @abstractmethod
    def generate_signals(
        self, closes: np.ndarray, volumes: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """输入收盘价数组，返回信号数组 (1=买入, -1=卖出, 0=持有)。长度与 closes 一致。"""
        ...

    def generate_entry_exit_masks(
        self, closes: np.ndarray, volumes: Optional[np.ndarray] = None
    ):
        """便捷方法：将信号转为 (entry_mask, exit_mask) 供 _simulate_trades_from_masks 使用。"""
        signals = self.generate_signals(closes, volumes)
        return (signals == 1), (signals == -1)

    def generate_signal_events(
        self, closes: np.ndarray, volumes: Optional[np.ndarray] = None
    ) -> Optional[list[StrategySignalEvent]]:
        """可选 richer 接口：返回稀疏事件流，默认不启用。"""
        return None

    def generate_signal_events_from_klines(
        self, klines: list[dict]
    ) -> Optional[list[StrategySignalEvent]]:
        """可选 richer 接口：基于完整 OHLCV 返回事件流，默认不启用。"""
        return None
