"""截面统计助手。"""

from typing import Iterable

import numpy as np
from scipy import stats as sp_stats


def _clean(values: Iterable[float]) -> list[float]:
    result = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(numeric):
            result.append(numeric)
    return result


def build_cross_section_summary(current: float | None, peers: Iterable[float], higher_is_better: bool = True) -> dict:
    values = _clean(peers)
    if current is None:
        return {"rank": None, "total": len(values), "percentile": None, "median": None}
    if not np.isfinite(float(current)):
        return {"rank": None, "total": len(values), "percentile": None, "median": None}
    universe = values + [float(current)]
    ordered = sorted(universe, reverse=bool(higher_is_better))
    rank = ordered.index(float(current)) + 1 if ordered else None
    percentile = None
    if universe:
        percentile = round(float(sp_stats.percentileofscore(universe, float(current), kind="rank")), 1)
    median = round(float(np.median(universe)), 4) if universe else None
    return {"rank": rank, "total": len(universe), "percentile": percentile, "median": median}