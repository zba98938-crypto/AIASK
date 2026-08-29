"""因子衰减追踪器 — 持续监控活跃因子的 IC 变化。

参考：Alpha Decay Modeling (arXiv:2512.11913) — 双曲衰减 α(t) = K/(1+λt)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class DecayMeasurement:
    """单次衰减测量。"""
    factor_id: str
    measured_at: str
    admission_ic: float
    current_ic: float
    decay_rate: float
    estimated_half_life_days: float | None = None
    decay_model: str = "hyperbolic"  # hyperbolic / linear / exponential

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "measured_at": self.measured_at,
            "admission_ic": round(self.admission_ic, 6),
            "current_ic": round(self.current_ic, 6),
            "decay_rate": round(self.decay_rate, 6),
            "estimated_half_life_days": (
                round(self.estimated_half_life_days, 1)
                if self.estimated_half_life_days is not None
                else None
            ),
            "decay_model": self.decay_model,
        }


@dataclass
class DecayHistory:
    """因子衰减历史。"""
    factor_id: str
    measurements: list[DecayMeasurement] = field(default_factory=list)

    @property
    def latest_decay_rate(self) -> float:
        if not self.measurements:
            return 0.0
        return self.measurements[-1].decay_rate

    @property
    def trend(self) -> str:
        """衰减趋势：accelerating / stable / recovering。"""
        if len(self.measurements) < 3:
            return "insufficient_data"
        recent = [m.decay_rate for m in self.measurements[-5:]]
        if all(recent[i] >= recent[i - 1] for i in range(1, len(recent))):
            return "accelerating"
        if all(recent[i] <= recent[i - 1] for i in range(1, len(recent))):
            return "recovering"
        return "stable"


class DecayTracker:
    """因子衰减追踪器。

    使用双曲衰减模型 α(t) = K/(1+λt) 拟合因子 IC 衰减曲线。
    """

    def __init__(self):
        self._histories: dict[str, DecayHistory] = {}

    def measure_decay(
        self,
        factor_id: str,
        admission_ic: float,
        current_ic: float,
        days_since_admission: int = 0,
    ) -> DecayMeasurement:
        """测量单个因子的衰减。"""
        # 计算衰减率
        if abs(admission_ic) < 1e-8:
            decay_rate = 0.0
        else:
            decay_rate = max(0.0, 1.0 - abs(current_ic) / abs(admission_ic))

        # 估计半衰期（双曲衰减模型）
        half_life = self._estimate_half_life_hyperbolic(
            admission_ic, current_ic, days_since_admission
        )

        measurement = DecayMeasurement(
            factor_id=factor_id,
            measured_at=datetime.now(timezone.utc).isoformat(),
            admission_ic=admission_ic,
            current_ic=current_ic,
            decay_rate=decay_rate,
            estimated_half_life_days=half_life,
            decay_model="hyperbolic",
        )

        # 记录历史
        if factor_id not in self._histories:
            self._histories[factor_id] = DecayHistory(factor_id=factor_id)
        self._histories[factor_id].measurements.append(measurement)

        # 保留最近 60 条
        if len(self._histories[factor_id].measurements) > 60:
            self._histories[factor_id].measurements = self._histories[factor_id].measurements[-60:]

        return measurement

    def get_history(self, factor_id: str) -> DecayHistory | None:
        """获取因子衰减历史。"""
        return self._histories.get(factor_id)

    def get_all_decay_rates(self) -> dict[str, float]:
        """获取所有因子的最新衰减率。"""
        return {
            fid: history.latest_decay_rate
            for fid, history in self._histories.items()
        }

    @staticmethod
    def _estimate_half_life_hyperbolic(
        admission_ic: float,
        current_ic: float,
        days: int,
    ) -> float | None:
        """双曲衰减模型估计半衰期。

        模型：α(t) = K / (1 + λt)
        半衰期：t_half = 1/λ
        """
        if days <= 0 or abs(admission_ic) < 1e-8:
            return None

        ratio = abs(current_ic) / abs(admission_ic)
        if ratio >= 1.0 or ratio <= 0.0:
            return None

        # 从 α(t)/α(0) = 1/(1+λt) 解出 λ
        # λ = (1/ratio - 1) / t
        try:
            lambda_param = (1.0 / ratio - 1.0) / days
            if lambda_param <= 0:
                return None
            half_life = 1.0 / lambda_param
            return half_life if math.isfinite(half_life) and half_life > 0 else None
        except (ZeroDivisionError, ValueError):
            return None
