"""单因子策略族 — 基于技术面近似的因子排名信号

提供 SingleFactorStrategy 抽象基类和三个具体子类:
- ValueFactorStrategy: 价值因子（负动量 + 均值回归）
- QualityFactorStrategy: 质量因子（收益稳定性 + 低波动）
- GrowthFactorStrategy: 成长因子（收益加速度）

所有策略仅使用 closes/volumes 作为输入，通过技术面指标近似基本面因子。
"""

from abc import abstractmethod
from typing import Any, Dict, Optional

import numpy as np

from .strategy_base import IStrategy


class SingleFactorStrategy(IStrategy):
    """单因子策略抽象基类 — 滚动窗口内按因子分位数排名生成信号"""

    def __init__(self):
        self._lookback = 60
        self._buy_quantile = 0.8
        self._sell_quantile = 0.2
        # PR-S16: 可选的 financial snapshot，由 params['financial_snapshot'] 注入
        # snapshot 字段示例：{"pe_ratio": 23.1, "pb_ratio": 8.4, "roe": 0.31,
        #                    "revenue_growth_yoy": 0.12, "gross_margin": 0.54,
        #                    "debt_ratio": 0.21, "net_profit_growth_yoy": 0.18}
        self._financial_snapshot: Dict[str, Any] = {}

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "lookback": self._lookback,
            "buy_quantile": self._buy_quantile,
            "sell_quantile": self._sell_quantile,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self._lookback = max(10, int(params.get("lookback", self._lookback)))
        self._buy_quantile = float(params.get("buy_quantile", self._buy_quantile))
        self._sell_quantile = float(params.get("sell_quantile", self._sell_quantile))
        # PR-S16: 接收上游注入的 financials
        snapshot = params.get("financial_snapshot") or params.get("financials")
        if isinstance(snapshot, dict):
            self._financial_snapshot = dict(snapshot)
        else:
            self._financial_snapshot = {}

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _fundamental_score(self, kind: str) -> Optional[float]:
        """PR-S16: 从 financial_snapshot 抽取真实基本面分数。

        kind ∈ {"value", "quality", "growth"}。返回 None 时 caller 应降级到技术面近似。
        """
        snap = self._financial_snapshot or {}
        if not snap:
            return None
        if kind == "value":
            pe = self._safe_float(snap.get("pe_ratio"))
            pb = self._safe_float(snap.get("pb_ratio"))
            if pe is None and pb is None:
                return None
            # 真实价值：PE / PB 倒数加权（低估值得分高）
            pe_score = (1.0 / pe) if (pe and pe > 0) else 0.0
            pb_score = (1.0 / pb) if (pb and pb > 0) else 0.0
            return 0.6 * pe_score + 0.4 * pb_score
        if kind == "quality":
            roe = self._safe_float(snap.get("roe"))
            gm = self._safe_float(snap.get("gross_margin"))
            debt = self._safe_float(snap.get("debt_ratio"))
            if roe is None and gm is None:
                return None
            roe_term = roe if roe is not None else 0.0
            gm_term = gm if gm is not None else 0.0
            debt_term = (1.0 - debt) if debt is not None else 0.0
            return 0.5 * roe_term + 0.3 * gm_term + 0.2 * debt_term
        if kind == "growth":
            rev_growth = self._safe_float(snap.get("revenue_growth_yoy"))
            np_growth = self._safe_float(snap.get("net_profit_growth_yoy"))
            if rev_growth is None and np_growth is None:
                return None
            rev_term = rev_growth if rev_growth is not None else 0.0
            np_term = np_growth if np_growth is not None else 0.0
            return 0.5 * rev_term + 0.5 * np_term
        return None

    @abstractmethod
    def _compute_factor(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        """计算因子值序列，返回与 closes 等长的数组（NaN 表示无效）"""
        ...

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        n = len(closes)
        signals = np.zeros(n, dtype=np.int8)
        if n < self._lookback + 2:
            return signals
        factor = self._compute_factor(closes, volumes)
        for i in range(self._lookback, n):
            window = factor[max(0, i - self._lookback):i + 1]
            valid = window[np.isfinite(window)]
            if len(valid) < 5:
                continue
            rank = float(np.sum(valid < factor[i])) / len(valid)
            if rank >= self._buy_quantile:
                signals[i] = 1
            elif rank <= self._sell_quantile:
                signals[i] = -1
        return signals


class ValueFactorStrategy(SingleFactorStrategy):
    """价值因子 — 负动量 + 均值回归偏离度（技术面近似）"""

    @classmethod
    def name(cls) -> str:
        return "value_factor"

    @classmethod
    def description(cls) -> str:
        return "价值因子策略：低动量+均值回归倾向越强，价值得分越高"

    def _compute_factor(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        n = len(closes)
        factor = np.full(n, np.nan)
        lb = min(self._lookback, 60)
        # PR-S16: 优先用真实 PE/PB
        fundamental = self._fundamental_score("value")
        for i in range(lb, n):
            ret = (closes[i] - closes[i - lb]) / closes[i - lb] if closes[i - lb] > 0 else 0
            ma = float(np.mean(closes[i - lb:i + 1]))
            deviation = (closes[i] - ma) / ma if ma > 0 else 0
            technical = -(ret * 0.5 + deviation * 0.5)
            if fundamental is not None:
                # 70% 基本面 + 30% 技术面（保留技术面捕捉时点）
                factor[i] = 0.7 * fundamental + 0.3 * technical
            else:
                factor[i] = technical
        return factor


class QualityFactorStrategy(SingleFactorStrategy):
    """质量因子 — 收益稳定性 + 低波动（技术面近似）"""

    @classmethod
    def name(cls) -> str:
        return "quality_factor"

    @classmethod
    def description(cls) -> str:
        return "质量因子策略：收益稳定、波动低的股票得分高"

    def _compute_factor(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        n = len(closes)
        factor = np.full(n, np.nan)
        lb = min(self._lookback, 60)
        # PR-S16: 优先用真实 ROE / 毛利率 / 负债
        fundamental = self._fundamental_score("quality")
        for i in range(lb, n):
            window = closes[i - lb:i + 1]
            rets = np.diff(window) / window[:-1]
            if len(rets) < 2:
                continue
            stability = -float(np.std(rets))
            positive_ratio = float(np.mean(rets > 0))
            technical = stability * 0.6 + positive_ratio * 0.4
            if fundamental is not None:
                factor[i] = 0.7 * fundamental + 0.3 * technical
            else:
                factor[i] = technical
        return factor


class GrowthFactorStrategy(SingleFactorStrategy):
    """成长因子 — 收益加速度（技术面近似）"""

    @classmethod
    def name(cls) -> str:
        return "growth_factor"

    @classmethod
    def description(cls) -> str:
        return "成长因子策略：近期收益加速的股票得分高"

    def _compute_factor(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        n = len(closes)
        factor = np.full(n, np.nan)
        lb = min(self._lookback, 60)
        # PR-S16: 优先用真实营收/净利润同比增长率
        fundamental = self._fundamental_score("growth")
        for i in range(lb, n):
            ret_short = (closes[i] - closes[i - 20]) / closes[i - 20] if i >= 20 and closes[i - 20] > 0 else 0
            ret_long = (closes[i] - closes[i - lb]) / closes[i - lb] if closes[i - lb] > 0 else 0
            technical = ret_short - ret_long / max(lb / 20, 1)
            if fundamental is not None:
                factor[i] = 0.7 * fundamental + 0.3 * technical
            else:
                factor[i] = technical
        return factor