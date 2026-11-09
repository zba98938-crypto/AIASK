"""多因子策略 — 加权合成多个技术面因子的复合信号"""

from typing import Any, Dict, Optional

import numpy as np

from .strategy_base import IStrategy


# 因子名 → 策略类的延迟映射（避免循环导入）
_FACTOR_MAP = None


def _get_factor_map():
    global _FACTOR_MAP
    if _FACTOR_MAP is None:
        from .single_factor_strategy import (
            ValueFactorStrategy, QualityFactorStrategy, GrowthFactorStrategy,
        )
        _FACTOR_MAP = {
            "value": ValueFactorStrategy,
            "quality": QualityFactorStrategy,
            "growth": GrowthFactorStrategy,
        }
    return _FACTOR_MAP


class MultiFactorStrategy(IStrategy):
    """多因子策略 — 加权合成多个因子的z-score，按分位数生成信号"""

    def __init__(self):
        self._factor_weights: Dict[str, float] = {
            "value": 0.33, "quality": 0.34, "growth": 0.33,
        }
        self._lookback = 60
        self._buy_quantile = 0.8
        self._sell_quantile = 0.2

    @classmethod
    def name(cls) -> str:
        return "multi_factor"

    @classmethod
    def description(cls) -> str:
        return "多因子策略：加权合成价值/质量/成长因子，综合排名选股"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "factor_weights": self._factor_weights.copy(),
            "lookback": self._lookback,
            "buy_quantile": self._buy_quantile,
            "sell_quantile": self._sell_quantile,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        fw = params.get("factor_weights")
        if isinstance(fw, dict) and fw:
            unsupported = sorted(str(name) for name, weight in fw.items()
                                 if weight != 0 and name not in _get_factor_map())
            if unsupported:
                raise ValueError("unsupported_factor_weights:" + ",".join(unsupported))
            total = sum(fw.values()) or 1.0
            self._factor_weights = {k: v / total for k, v in fw.items()}
        self._lookback = max(10, int(params.get("lookback", self._lookback)))
        self._buy_quantile = float(params.get("buy_quantile", self._buy_quantile))
        self._sell_quantile = float(params.get("sell_quantile", self._sell_quantile))

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        n = len(closes)
        signals = np.zeros(n, dtype=np.int8)
        if n < self._lookback + 2:
            return signals

        fmap = _get_factor_map()
        composite = np.zeros(n)
        complete = np.ones(n, dtype=bool)
        total_weight = 0.0

        for fname, weight in self._factor_weights.items():
            if weight == 0:
                continue
            klass = fmap.get(fname)
            if klass is None:
                raise ValueError("unsupported_factor_weights:" + str(fname))
            inst = klass()
            inst.set_parameters({"lookback": self._lookback})
            fvals = inst._compute_factor(closes, volumes)
            # Each observation uses only its own trailing history. Future
            # variance must not change past scaling or enable earlier trades.
            normed = np.full(n, np.nan)
            for i in range(n):
                if not np.isfinite(fvals[i]):
                    continue
                window = fvals[max(0, i - self._lookback + 1):i + 1]
                valid = window[np.isfinite(window)]
                if len(valid) < 10:
                    continue
                std_v = float(np.std(valid))
                if std_v > 0:
                    normed[i] = (fvals[i] - float(np.mean(valid))) / std_v
            available = np.isfinite(normed)
            complete &= available
            composite[available] += weight * normed[available]
            total_weight += weight

        if total_weight <= 0:
            return signals

        composite /= total_weight
        composite[~complete] = np.nan

        for i in range(self._lookback, n):
            if not np.isfinite(composite[i]):
                continue
            window = composite[max(0, i - self._lookback):i + 1]
            valid = window[np.isfinite(window)]
            if len(valid) < 5:
                continue
            rank = float(np.sum(valid < composite[i])) / len(valid)
            if rank >= self._buy_quantile:
                signals[i] = 1
            elif rank <= self._sell_quantile:
                signals[i] = -1

        return signals
