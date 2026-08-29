"""宏观择时策略 — 从价格序列估算恐贪指数进行择时"""

from typing import Any, Dict, Optional

import numpy as np

from .strategy_base import IStrategy


class MacroTimingStrategy(IStrategy):
    """宏观择时 — 从价格动量+波动率估算恐贪指数，低于恐惧阈值买入，高于贪婪阈值卖出"""

    def __init__(self):
        self._fear_threshold = 28
        self._greed_threshold = 72
        self._lookback = 30

    @classmethod
    def name(cls) -> str:
        return "macro_timing"

    @classmethod
    def description(cls) -> str:
        return "宏观择时策略：估算恐贪指数，恐惧时买入、贪婪时卖出"

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "fear_threshold": self._fear_threshold,
            "greed_threshold": self._greed_threshold,
            "lookback": self._lookback,
        }

    def set_parameters(self, params: Dict[str, Any]) -> None:
        self._fear_threshold = int(params.get("fear_threshold", self._fear_threshold))
        self._greed_threshold = int(params.get("greed_threshold", self._greed_threshold))
        self._lookback = max(5, int(params.get("lookback", self._lookback)))

    def _estimate_fear_greed(self, window: np.ndarray, vol_window: Optional[np.ndarray] = None) -> float:
        """从价格窗口估算恐贪指数 0-100"""
        if len(window) < 5:
            return 50.0
        rets = np.diff(window) / window[:-1]
        # 动量分量: 近5日收益率映射到 0-100
        momentum = 50.0 + float(np.mean(rets[-5:])) * 400.0
        # 波动率分量: 高波动 → 恐惧 → 低分；低波动 → 贪婪 → 高分
        vol = float(np.std(rets)) * np.sqrt(252)
        vol_score = 75.0 - vol * 800.0
        # 成交量分量（如果有）
        volume_score = 50.0
        if vol_window is not None and len(vol_window) >= 15:
            short_v = float(np.mean(vol_window[-5:]))
            long_v = float(np.mean(vol_window[-15:-5]))
            if long_v > 0:
                volume_score = 50.0 + (short_v / long_v - 1.0) * 25.0
        composite = (momentum + vol_score + volume_score) / 3.0
        return float(np.clip(composite, 0, 100))

    def generate_signals(self, closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> np.ndarray:
        n = len(closes)
        signals = np.zeros(n, dtype=np.int8)
        lb = self._lookback
        if n < lb + 1:
            return signals
        for i in range(lb, n):
            price_win = closes[i - lb:i + 1]
            vol_win = volumes[i - lb:i + 1] if volumes is not None else None
            fg = self._estimate_fear_greed(price_win, vol_win)
            prev_fg = fg
            if i > lb:
                prev_price_win = closes[i - lb:i]
                prev_vol_win = volumes[i - lb:i] if volumes is not None else None
                prev_fg = self._estimate_fear_greed(prev_price_win, prev_vol_win)
            tail = price_win[-5:] if len(price_win) >= 5 else price_win
            recent_floor = float(np.min(tail)) if len(tail) > 0 else float(closes[i])
            rebound_ratio = ((float(closes[i]) - recent_floor) / recent_floor) if recent_floor > 0 else 0.0
            rolling_mean = float(np.mean(price_win[:-1])) if len(price_win) > 1 else float(closes[i])
            stabilization = rolling_mean <= 0 or float(closes[i]) >= rolling_mean * 0.97
            if fg <= self._fear_threshold and fg >= prev_fg and rebound_ratio >= 0.01 and stabilization:
                signals[i] = 1
            elif fg >= self._greed_threshold or (
                fg >= 55 and rolling_mean > 0 and float(closes[i]) < rolling_mean * 0.98
            ):
                signals[i] = -1
        return signals
