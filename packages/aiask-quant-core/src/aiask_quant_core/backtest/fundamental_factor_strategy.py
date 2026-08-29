"""PR-B1: 真实基本面多因子策略。

使用 financials 表中的 PE/PB/ROE/营收增速等真实数据，
结合技术面动量确认，生成买卖信号。

与 multi_factor_strategy.py 的区别：
- 旧版用价格近似（reversal = "价值"，低波动 = "质量"，动量加速 = "成长"）
- 本版用真实基本面数据（PE/PB/ROE/revenue_growth），技术面仅做确认
"""

from typing import Any, Dict, Optional

import numpy as np

from .strategy_base import IStrategy


class FundamentalMultiFactorStrategy(IStrategy):
    """真实基本面多因子策略 — 使用 financials 表数据。

    params 中需要 fundamentals_snapshot:
    {
        "pe_ttm": float,
        "pb_mrq": float,
        "roe_ttm": float,
        "revenue_growth": float,
        "profit_growth": float,
    }
    """

    def __init__(self):
        self._weights = {
            "value": 0.25,
            "quality": 0.30,
            "growth": 0.25,
            "momentum": 0.20,
        }
        self._buy_threshold = 0.55
        self._sell_threshold = 0.30
        self._lookback = 20
        self._fundamentals: Dict[str, Any] = {}

    @classmethod
    def name(cls) -> str:
        return "fundamental_multi_factor"

    @classmethod
    def description(cls) -> str:
        return "真实基本面多因子策略：使用 PE/PB/ROE/营收增速 + 技术面动量确认"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "factor_weights": self._weights.copy(),
            "buy_threshold": self._buy_threshold,
            "sell_threshold": self._sell_threshold,
            "lookback": self._lookback,
            "fundamentals_snapshot": self._fundamentals.copy(),
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        fw = params.get("factor_weights")
        if isinstance(fw, dict) and fw:
            total = sum(fw.values()) or 1.0
            self._weights = {k: v / total for k, v in fw.items()}
        self._buy_threshold = float(params.get("buy_threshold", self._buy_threshold))
        self._sell_threshold = float(params.get("sell_threshold", self._sell_threshold))
        self._lookback = max(10, int(params.get("lookback", self._lookback)))
        self._fundamentals = dict(params.get("fundamentals_snapshot") or {})

    def _value_score(self) -> float:
        """价值因子：PE 和 PB 在合理区间得分高。"""
        pe = float(self._fundamentals.get("pe_ttm") or 0)
        pb = float(self._fundamentals.get("pb_mrq") or 0)
        score = 0.0
        # PE: 0-15 满分，15-30 线性衰减，>30 或 <0 零分
        if 0 < pe <= 15:
            score += 0.5
        elif 15 < pe <= 30:
            score += 0.5 * (30 - pe) / 15
        # PB: 0-2 满分，2-5 线性衰减
        if 0 < pb <= 2:
            score += 0.5
        elif 2 < pb <= 5:
            score += 0.5 * (5 - pb) / 3
        return score

    def _quality_score(self) -> float:
        """质量因子：ROE 越高越好，>15% 满分。"""
        roe = float(self._fundamentals.get("roe_ttm") or 0)
        if roe <= 0:
            return 0.0
        return min(1.0, roe / 0.15)

    def _growth_score(self) -> float:
        """成长因子：营收增速和利润增速。"""
        rev_g = float(self._fundamentals.get("revenue_growth") or 0)
        profit_g = float(self._fundamentals.get("profit_growth") or 0)
        rev_score = min(1.0, max(0.0, rev_g / 0.30)) if rev_g > 0 else 0.0
        profit_score = min(1.0, max(0.0, profit_g / 0.30)) if profit_g > 0 else 0.0
        return rev_score * 0.5 + profit_score * 0.5

    def _momentum_score(self, closes: np.ndarray, i: int) -> float:
        """动量确认：20 日收益率为正且高于均线。"""
        lb = self._lookback
        if i < lb:
            return 0.0
        ret = (closes[i] - closes[i - lb]) / closes[i - lb] if closes[i - lb] > 0 else 0
        ma = float(np.mean(closes[max(0, i - lb):i + 1]))
        above_ma = closes[i] > ma
        score = 0.0
        if ret > 0:
            score += 0.5
        if above_ma:
            score += 0.5
        return score

    def generate_signals(
        self, closes: np.ndarray, volumes: Optional[np.ndarray] = None
    ) -> np.ndarray:
        n = len(closes)
        signals = np.zeros(n, dtype=np.int8)
        if n < self._lookback + 2:
            return signals

        # 基本面分数（整个回测期间不变，因为 financials 是季度数据）
        value = self._value_score()
        quality = self._quality_score()
        growth = self._growth_score()

        # 基本面综合分（不含动量）
        fundamental_composite = (
            self._weights["value"] * value
            + self._weights["quality"] * quality
            + self._weights["growth"] * growth
        )
        fundamental_weight = (
            self._weights["value"] + self._weights["quality"] + self._weights["growth"]
        )

        for i in range(self._lookback, n):
            momentum = self._momentum_score(closes, i)
            # 综合分 = 基本面 + 动量确认
            composite = fundamental_composite + self._weights["momentum"] * momentum
            total_weight = fundamental_weight + self._weights["momentum"]
            normalized = composite / total_weight if total_weight > 0 else 0.0

            if normalized >= self._buy_threshold and momentum > 0.3:
                # 买入条件：综合分达标 + 动量确认（避免在下跌趋势中抄底）
                signals[i] = 1
            elif normalized <= self._sell_threshold or momentum == 0:
                # 卖出条件：综合分过低 或 动量消失
                if i > 0 and signals[i - 1] != -1:
                    # 只在持仓时卖出（避免连续卖出信号）
                    signals[i] = -1

        return signals
