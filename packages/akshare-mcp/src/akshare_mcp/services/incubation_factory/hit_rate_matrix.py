"""P3-1：命中率矩阵（strategy_type × regime × holding_bucket）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 3 · P3-1
目标：回答"AI 在什么股票、什么 regime、什么周期下生成的什么类型策略，前向命中率多少"。

设计要点：
- **聚合到"类型 × regime × 周期"层级**（非逐股），否则 5000 股 × 多类型 × 多周期单元稀释成空表。
- 数据源：对每只 incubating 策略跑 ForwardVerifier.verify → 取 hit_rate_by_regime + strategy_type + holding_bucket。
- **空单元诚实标注** `insufficient_samples`，绝不伪造（min_n 门槛）。
- 依赖注入：strategy_lister + verifier 可注入，单测无需 DB/网络。
- skill_lcb 复用 Wilson 下界（按命中序列重算需要原始 hits；此处对已聚合的 cell 用样本量加权合并）。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable, Optional

import numpy as np

from .forward_verifier import REGIME_DIMENSIONS, _REGIME_UNKNOWN

logger = logging.getLogger(__name__)

# 单元最小样本量门槛（低于此值标注 insufficient_samples，不报命中率）。
DEFAULT_MIN_CELL_N = 5


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _safe_int(value: Any, default: int = 0) -> int:
    numeric = _finite_float(value)
    if numeric is not None:
        return int(numeric)
    fallback = _finite_float(default)
    return int(fallback if fallback is not None else 0)

# 类型签名
StrategyLister = Callable[[str, int], Awaitable[list[dict[str, Any]]]]  # (status, limit) -> rows
Verifier = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]         # (strategy) -> verify result


def _holding_bucket_of(strategy: dict[str, Any]) -> str:
    return (
        str(
            strategy.get("holding_period_bucket")
            or strategy.get("holding_bucket")
            or "unknown"
        )
        .strip()
        .lower()
        or "unknown"
    )


def _strategy_type_of(strategy: dict[str, Any]) -> str:
    return (
        str(
            strategy.get("strategy_type")
            or strategy.get("candidate_family")
            or "unknown"
        )
        .strip()
        .lower()
        or "unknown"
    )


class _CellAccumulator:
    """累计某个 (type, regime_dim, regime_label, bucket) 单元的命中样本。

    用样本量加权合并各策略的 hit_rate；skill_lcb 按合并 hit_rate + 总 n 重算 Wilson 下界。
    """

    __slots__ = ("hit_weighted", "n")

    def __init__(self) -> None:
        self.hit_weighted = 0.0  # sum(hit_rate_i * n_i)
        self.n = 0

    def add(self, hit_rate: float, n: int) -> None:
        safe_n = _safe_int(n, 0)
        safe_hit_rate = _finite_float(hit_rate)
        if safe_n <= 0 or safe_hit_rate is None:
            return
        safe_hit_rate = max(0.0, min(1.0, safe_hit_rate))
        self.hit_weighted += safe_hit_rate * safe_n
        self.n += safe_n

    def summary(self, *, min_n: int) -> dict[str, Any]:
        if self.n < min_n:
            return {"status": "insufficient_samples", "n": self.n}
        hit_rate = self.hit_weighted / self.n if self.n else 0.0
        return {
            "status": "ok",
            "hit_rate": round(hit_rate, 4),
            "skill_lcb": round(_wilson_skill_lcb(hit_rate, self.n), 4),
            "n": self.n,
        }


def _wilson_skill_lcb(hit_rate: float, n: int) -> float:
    """Wilson 下界 - 0.5 随机基线（与 ForwardVerifier._compute_skill_lcb 同口径）。"""
    safe_hit_rate = _finite_float(hit_rate)
    safe_n = _safe_int(n, 0)
    if safe_hit_rate is None or safe_n < 5:
        return 0.0
    hit_rate = max(0.0, min(1.0, safe_hit_rate))
    n = safe_n
    z = 1.96
    denominator = 1.0 + z * z / n
    center = (hit_rate + z * z / (2.0 * n)) / denominator
    margin = z * np.sqrt((hit_rate * (1.0 - hit_rate) + z * z / (4.0 * n)) / n) / denominator
    return float(center - margin - 0.5)


def aggregate_hit_rate_matrix(
    verify_results: list[dict[str, Any]],
    strategies_by_id: dict[str, dict[str, Any]],
    *,
    min_cell_n: int = DEFAULT_MIN_CELL_N,
) -> dict[str, Any]:
    """把多条 verify 结果交叉聚合成 type × regime × bucket 矩阵（纯函数）。

    返回结构：
    {
      "matrix": {
        strategy_type: {
          holding_bucket: {
            regime_dimension: { regime_label: {status/hit_rate/skill_lcb/n} }
          }
        }
      },
      "totals": {"strategies": int, "cells_ok": int, "cells_insufficient": int},
      "min_cell_n": int,
    }
    """
    # cells[(type, bucket, dim, label)] -> _CellAccumulator
    cells: dict[tuple, _CellAccumulator] = {}
    seen_types: set[str] = set()
    seen_buckets_by_type: dict[str, set[str]] = {}

    for result in verify_results or []:
        sid = str(result.get("strategy_id") or "").strip()
        strategy = strategies_by_id.get(sid, {})
        stype = _strategy_type_of({**strategy, "strategy_type": result.get("strategy_type") or strategy.get("strategy_type")})
        bucket = _holding_bucket_of(strategy)
        seen_types.add(stype)
        seen_buckets_by_type.setdefault(stype, set()).add(bucket)

        hrbr = dict(result.get("hit_rate_by_regime") or {})
        for dimension in REGIME_DIMENSIONS:
            dim_summary = dict(hrbr.get(dimension) or {})
            for label, cell in dim_summary.items():
                if not isinstance(cell, dict):
                    continue
                hit_rate = _finite_float(cell.get("hit_rate"))
                n = _safe_int(cell.get("n"), 0)
                if hit_rate is None:
                    continue
                key = (stype, bucket, dimension, str(label or _REGIME_UNKNOWN))
                cells.setdefault(key, _CellAccumulator()).add(hit_rate, n)

    matrix: dict[str, Any] = {}
    cells_ok = 0
    cells_insufficient = 0
    for (stype, bucket, dimension, label), acc in cells.items():
        summary = acc.summary(min_n=min_cell_n)
        if summary.get("status") == "ok":
            cells_ok += 1
        else:
            cells_insufficient += 1
        (
            matrix
            .setdefault(stype, {})
            .setdefault(bucket, {})
            .setdefault(dimension, {})
        )[label] = summary

    return {
        "matrix": matrix,
        "totals": {
            "strategies": len(verify_results or []),
            "types": len(seen_types),
            "cells_ok": cells_ok,
            "cells_insufficient": cells_insufficient,
        },
        "min_cell_n": min_cell_n,
        "regime_dimensions": list(REGIME_DIMENSIONS),
    }


async def build_hit_rate_matrix(
    *,
    strategy_lister: StrategyLister,
    verifier: Verifier,
    statuses: tuple[str, ...] = ("incubating", "listed"),
    limit: int = 200,
    min_cell_n: int = DEFAULT_MIN_CELL_N,
) -> dict[str, Any]:
    """对各状态策略跑 verify → 聚合命中率矩阵。

    strategy_lister(status, limit) / verifier(strategy) 注入，便于单测与解耦。
    任一策略 verify 失败跳过（不阻断整张矩阵）。
    """
    strategies_by_id: dict[str, dict[str, Any]] = {}
    verify_results: list[dict[str, Any]] = []

    seen: set[str] = set()
    for status in statuses:
        try:
            rows = await strategy_lister(status, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hit_rate_matrix: list_strategies(%s) failed: %s", status, exc)
            rows = []
        for strategy in rows or []:
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            strategies_by_id[sid] = dict(strategy)
            try:
                result = await verifier(strategy)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hit_rate_matrix: verify failed for %s: %s", sid, exc)
                continue
            if isinstance(result, dict):
                result.setdefault("strategy_id", sid)
                verify_results.append(result)

    return aggregate_hit_rate_matrix(verify_results, strategies_by_id, min_cell_n=min_cell_n)


__all__ = [
    "aggregate_hit_rate_matrix",
    "build_hit_rate_matrix",
    "DEFAULT_MIN_CELL_N",
]
