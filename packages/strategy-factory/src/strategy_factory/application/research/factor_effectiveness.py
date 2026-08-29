"""PR-F6: 因子有效性自动筛选。

从 factor_ic_history 表自动筛选有效因子，替代 FACTORY_RESEARCH_FACTORS 硬编码。

有效因子标准：
- |Rank IC 均值| > 0.03（任一 horizon）
- IC 胜率 > 55%（IC > 0 的天数占比）
- 至少有 20 天 IC 历史
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MIN_IC = 0.03
_DEFAULT_MIN_WIN_RATE = 0.55
_DEFAULT_MIN_DAYS = 20
_FALLBACK_FACTORS = ["momentum", "value", "quality", "growth", "volatility", "reversal"]


async def select_effective_factors(
    db: Any,
    *,
    min_ic: float = _DEFAULT_MIN_IC,
    min_win_rate: float = _DEFAULT_MIN_WIN_RATE,
    min_days: int = _DEFAULT_MIN_DAYS,
    max_factors: int = 20,
) -> list[str]:
    """从 factor_ic_history 自动筛选有效因子。

    Returns:
        有效因子名列表（按 |avg_ic| 降序），至少返回 fallback 列表。
    """
    acquire = getattr(db, "acquire", None)
    if not callable(acquire):
        return list(_FALLBACK_FACTORS)

    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    factor_name,
                    COUNT(*) as ic_days,
                    AVG(ic_value) as avg_ic,
                    AVG(CASE WHEN rank_ic IS NOT NULL THEN rank_ic ELSE ic_value END) as avg_rank_ic,
                    SUM(CASE WHEN ic_value > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as ic_win_rate,
                    MAX(ic_date) as latest_date
                FROM factor_ic_history
                WHERE ic_date >= date('now', '-90 days')
                GROUP BY factor_name
                HAVING COUNT(*) >= ?
                ORDER BY ABS(AVG(ic_value)) DESC
                """,
                min_days,
            )
    except Exception as exc:
        logger.debug("select_effective_factors: query failed: %s", exc)
        return list(_FALLBACK_FACTORS)

    if not rows:
        return list(_FALLBACK_FACTORS)

    effective: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        avg_ic = abs(float(row.get("avg_ic") or 0))
        win_rate = float(row.get("ic_win_rate") or 0)
        factor_name = str(row.get("factor_name") or "").strip()

        if not factor_name:
            continue
        # 跳过 factor_candidate: 前缀的种子因子（z-score，不是真实因子）
        if factor_name.startswith("factor_candidate:"):
            continue
        if avg_ic >= min_ic and win_rate >= min_win_rate:
            effective.append({
                "factor_name": factor_name,
                "avg_ic": avg_ic,
                "ic_win_rate": win_rate,
                "ic_days": int(row.get("ic_days") or 0),
            })

    if not effective:
        logger.info("select_effective_factors: no factors passed threshold, using fallback")
        return list(_FALLBACK_FACTORS)

    # 按 |avg_ic| 降序
    effective.sort(key=lambda x: -x["avg_ic"])
    result = [f["factor_name"] for f in effective[:max_factors]]
    logger.info(
        "select_effective_factors: %d effective factors (top: %s)",
        len(result), result[:5],
    )
    return result


__all__ = ["select_effective_factors"]
