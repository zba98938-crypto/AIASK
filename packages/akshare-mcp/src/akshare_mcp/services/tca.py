"""TCA (Transaction Cost Analysis) 服务 — 统一交易成本估算。

提供三种滑点模型 + A股标准费率计算，供回测引擎和撮合引擎共用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Any

from .slippage import (
    FixedSlippageModel,
    VolumeBasedSlippageModel,
    MarketImpactModel,
)

# ── A 股标准费率 ──────────────────────────────────────────────
COMMISSION_RATE = 0.00025        # 佣金 0.025%
COMMISSION_MIN = 5.0             # 最低佣金 5 元
STAMP_TAX_RATE = 0.0005          # 印花税 0.05%（仅卖出）
TRANSFER_FEE_RATE = 0.00001     # 过户费 0.001%


@dataclass
class TCAReport:
    """交易成本分析报告"""
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    slippage: Dict[str, float] = field(default_factory=dict)
    total_cost: float = 0.0
    cost_rate: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commission": round(self.commission, 4),
            "stamp_tax": round(self.stamp_tax, 4),
            "transfer_fee": round(self.transfer_fee, 4),
            "slippage": {k: round(v, 4) for k, v in self.slippage.items()},
            "total_cost": round(self.total_cost, 4),
            "cost_rate": round(self.cost_rate, 6),
        }


class TCAService:
    """交易成本分析服务"""

    def __init__(
        self,
        commission_rate: float = COMMISSION_RATE,
        commission_min: float = COMMISSION_MIN,
        stamp_tax_rate: float = STAMP_TAX_RATE,
        transfer_fee_rate: float = TRANSFER_FEE_RATE,
    ):
        self.commission_rate = commission_rate
        self.commission_min = commission_min
        self.stamp_tax_rate = stamp_tax_rate
        self.transfer_fee_rate = transfer_fee_rate
        self.models = {
            "fixed": FixedSlippageModel(slippage_rate=0.001),
            "volume": VolumeBasedSlippageModel(),
            "impact": MarketImpactModel(),
        }

    # ── 费率计算 ──────────────────────────────────────────────

    def _calc_commission(self, notional: float) -> float:
        return max(self.commission_min, notional * self.commission_rate)

    def _calc_stamp_tax(self, notional: float, is_sell: bool) -> float:
        return notional * self.stamp_tax_rate if is_sell else 0.0

    def _calc_transfer_fee(self, notional: float) -> float:
        return notional * self.transfer_fee_rate

    # ── 公开接口 ──────────────────────────────────────────────

    def estimate_cost(
        self,
        code: str,
        side: str,
        quantity: int,
        price: float,
        volume: float = 0,
    ) -> TCAReport:
        """估算单笔交易的全部成本。

        Args:
            code: 股票代码
            side: 'buy' | 'sell'
            quantity: 股数
            price: 委托价格
            volume: 当日成交量（用于动态滑点模型，0 则仅返回固定滑点）
        """
        is_sell = side == "sell"
        is_buy = not is_sell
        notional = price * quantity

        commission = self._calc_commission(notional)
        stamp_tax = self._calc_stamp_tax(notional, is_sell)
        transfer_fee = self._calc_transfer_fee(notional)

        slippage: Dict[str, float] = {}
        for name, model in self.models.items():
            slip = model.calculate_slippage(price, volume, quantity, is_buy)
            slippage[name] = abs(slip) * quantity

        # 总成本 = 固定费用 + 固定滑点模型估算
        fixed_slip = slippage.get("fixed", 0.0)
        total = commission + stamp_tax + transfer_fee + fixed_slip
        cost_rate = total / notional if notional > 0 else 0.0

        return TCAReport(
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage=slippage,
            total_cost=total,
            cost_rate=cost_rate,
        )

    def get_fee_schedule(self) -> Dict[str, Any]:
        """返回当前费率配置（供前端统一展示）"""
        return {
            "commission_rate": self.commission_rate,
            "commission_min": self.commission_min,
            "stamp_tax_rate": self.stamp_tax_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "slippage_models": list(self.models.keys()),
        }


# Singleton
_tca: TCAService | None = None


def get_tca_service() -> TCAService:
    global _tca
    if _tca is None:
        _tca = TCAService()
    return _tca
