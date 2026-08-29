"""Ranking service for strategy marketplace."""

from typing import Any, Iterable, List, Optional


DEFAULT_RANK_KEYS = [
    "sharpe_ratio",
    "total_return",
    "win_rate",
    "calmar_ratio",
    "max_drawdown",
]


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else default
    except Exception:
        return default


def rrf_rank(
    strategies: Iterable[dict],
    rank_keys: Optional[List[str]] = None,
    k: int = 60,
) -> List[dict]:
    """Reciprocal Rank Fusion across multiple metric dimensions."""
    rows = list(strategies or [])
    if not rows:
        return []

    keys = [str(x).strip() for x in (rank_keys or DEFAULT_RANK_KEYS) if str(x).strip()]
    if not keys:
        keys = list(DEFAULT_RANK_KEYS)

    scores = {str(s.get("id")): 0.0 for s in rows}
    for key in keys:
        reverse = key != "max_drawdown"
        sorted_ids = [
            str(s.get("id"))
            for s in sorted(rows, key=lambda x: _safe_float(x.get(key)), reverse=reverse)
        ]
        for rank, sid in enumerate(sorted_ids):
            if not sid:
                continue
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)

    enriched: List[dict] = []
    for s in rows:
        sid = str(s.get("id"))
        enriched.append({**s, "rrf_score": round(scores.get(sid, 0.0), 6)})

    return sorted(enriched, key=lambda x: _safe_float(x.get("rrf_score")), reverse=True)

