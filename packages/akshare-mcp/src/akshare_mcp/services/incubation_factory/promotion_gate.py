"""孵化工厂 · 晋升门（宿主侧：取数 + 决策再导出）。

晋升**决策**（DSR 多重检验校正）已迁入 Strategy Factory（Phase 3）——是否允许
晋升属生命周期决策，归 Strategy Factory 所有。DSR 统计实现取自
``aiask-quant-core``，无需宿主注入。

本模块保留两件事：

* ``fetch_forward_return_series`` —— 前向收益序列取数，属数据访问，留在宿主。
* 决策符号再导出 —— 使 ``from .promotion_gate import PromotionGate`` 这类既有
  导入无需改动；只经 ``strategy_factory.api`` 公共面获取。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from strategy_factory.api.runtime import (
    PROMOTION_DSR_ENV,
    PromotionGate,
    PromotionGateVerdict,
    promotion_gate_enabled,
)

logger = logging.getLogger(__name__)


def _finite_float(value: Any) -> Optional[float]:
    """本地数值清洗（取数路径自用，不依赖决策模块内部实现）。"""
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


# 兼容别名：既有调用方用下划线名引用该开关。
_promotion_gate_enabled = promotion_gate_enabled


async def fetch_forward_return_series(
    db: Any,
    strategy_id: str,
    *,
    horizon_days: int = 5,
    lookback_days: Optional[int] = None,
) -> list[float]:
    """从 signal_forward_returns 读取指定主窗口的前向收益序列。

    优先用 db 暴露的原始序列方法；若不可用则回退到 ``get_signal_stats`` 中的
    per-horizon 序列（best-effort）。两条路径都失败时返回空列表，由调用方按
    「样本不足」处理，而不是伪造数据。
    """
    getter = getattr(db, "list_signal_forward_returns", None)
    if callable(getter):
        try:
            rows = await getter(
                strategy_id, forward_days=horizon_days, lookback_days=lookback_days
            )
            values: list[float] = []
            for row in rows or []:
                numeric = _finite_float(dict(row or {}).get("actual_return"))
                if numeric is not None:
                    values.append(numeric)
            return values
        except Exception as exc:
            logger.debug("fetch_forward_return_series: list method failed: %s", exc)

    stats_getter = getattr(db, "get_signal_stats", None)
    if callable(stats_getter):
        try:
            stats = await stats_getter(strategy_id, lookback_days=lookback_days)
            series = dict(stats or {}).get("forward_return_series") or {}
            horizon_series = series.get(str(horizon_days)) or series.get(horizon_days)
            if horizon_series:
                values = []
                for item in horizon_series:
                    numeric = _finite_float(item)
                    if numeric is not None:
                        values.append(numeric)
                return values
        except Exception as exc:
            logger.debug("fetch_forward_return_series: stats fallback failed: %s", exc)
    return []


__all__ = [
    "PROMOTION_DSR_ENV",
    "PromotionGate",
    "PromotionGateVerdict",
    "_promotion_gate_enabled",
    "fetch_forward_return_series",
    "promotion_gate_enabled",
]
