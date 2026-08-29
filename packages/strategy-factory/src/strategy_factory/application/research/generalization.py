"""PR-B3: 多股票泛化验证。

通过 vector_profiles 找目标股票的 top-N 同行业/同因子暴露近邻，
对每只 peer 跑同参数回测，验证策略不是只在一只股票上有效。

通过条件：
- ≥ 60% 的 peer 上 Sharpe > 0
- peer 中位数 win_rate ≥ 0.45
- peer 中位数 profit_factor ≥ 1.3
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_DEFAULT_PEER_COUNT = 10
_MIN_PEERS_FOR_VALIDATION = 5
_MIN_KLINES_PER_PEER = 252


async def validate_generalization(
    candidate: dict[str, Any],
    db: Any,
    *,
    peer_count: int = _DEFAULT_PEER_COUNT,
) -> dict[str, Any]:
    """在同类股票上验证策略泛化性。

    Args:
        candidate: 策略候选（需含 target_symbols, strategy_type, params）
        db: 数据库适配器（需支持 list_vector_profiles, get_klines）
        peer_count: 查找的 peer 数量

    Returns:
        dict with passed, peer_count, positive_sharpe_ratio, median_win_rate, etc.
    """
    target_symbols = list(candidate.get("target_symbols") or [])
    if not target_symbols:
        return {"passed": False, "reason": "no_target_symbols"}

    target_code = str(target_symbols[0]).strip()
    strategy_type = str(candidate.get("strategy_type") or "").strip()
    params = dict(candidate.get("params") or {})

    if not target_code or not strategy_type:
        return {"passed": False, "reason": "missing_strategy_type_or_target"}

    # 通过 vector_profiles 找同类股票
    peers = await _find_peer_stocks(db, target_code, limit=peer_count)
    if len(peers) < _MIN_PEERS_FOR_VALIDATION:
        return {
            "passed": False,
            "reason": f"insufficient_peers ({len(peers)} < {_MIN_PEERS_FOR_VALIDATION})",
            "peer_count": len(peers),
        }

    # 对每只 peer 跑回测
    from aiask_quant_core.backtest import backtest_engine

    results: list[dict[str, Any]] = []
    for peer_code in peers:
        try:
            get_klines = getattr(db, "get_klines", None)
            if callable(get_klines):
                klines = await get_klines(peer_code, limit=750)
            else:
                continue
            if not klines or len(klines) < _MIN_KLINES_PER_PEER:
                continue
            r = backtest_engine.run_backtest(
                peer_code, list(klines), strategy_type, params
            )
            if r.get("success"):
                results.append(r["data"])
        except Exception as exc:
            logger.debug("generalization: backtest failed for %s: %s", peer_code, exc)
            continue

    if len(results) < _MIN_PEERS_FOR_VALIDATION:
        return {
            "passed": False,
            "reason": f"insufficient_successful_backtests ({len(results)} < {_MIN_PEERS_FOR_VALIDATION})",
            "peer_count": len(results),
        }

    sharpes = [float(r.get("sharpe_ratio") or 0.0) for r in results]
    win_rates = sorted(float(r.get("win_rate") or 0.0) for r in results)
    profit_factors = sorted(float(r.get("profit_factor") or 0.0) for r in results)
    returns = [float(r.get("total_return") or 0.0) for r in results]

    positive_sharpe_rate = sum(1 for s in sharpes if s > 0) / len(sharpes)
    median_wr = win_rates[len(win_rates) // 2]
    median_pf = profit_factors[len(profit_factors) // 2]
    median_return = sorted(returns)[len(returns) // 2]

    passed = (
        positive_sharpe_rate >= 0.6
        and median_wr >= 0.45
        and median_pf >= 1.3
    )

    return {
        "passed": passed,
        "peer_count": len(results),
        "target_code": target_code,
        "positive_sharpe_ratio": round(positive_sharpe_rate, 4),
        "median_win_rate": round(median_wr, 4),
        "median_profit_factor": round(median_pf, 4),
        "median_return": round(median_return, 4),
        "avg_sharpe": round(float(np.mean(sharpes)), 4),
        "peer_codes": peers[:10],
    }


async def _find_peer_stocks(
    db: Any,
    target_code: str,
    *,
    limit: int = 10,
) -> list[str]:
    """通过 stock_profile_embeddings 向量相似度找同类股票。"""
    search_fn = getattr(db, "search_vector_collection", None)
    list_fn = getattr(db, "list_vector_profiles", None)

    if not callable(list_fn):
        return []

    # 取目标股票的 embedding
    try:
        target_rows = await list_fn(
            collection_name="stock_profile_embeddings",
            stock_code=target_code,
            profile_type="both",
            limit=1,
        )
    except Exception:
        return []

    if not target_rows:
        return []

    import json as _json
    target_embedding = _json.loads(target_rows[0].get("embedding_json") or "[]")
    if not target_embedding or not callable(search_fn):
        # Fallback: 取同 collection 的随机 peer
        try:
            all_rows = await list_fn(
                collection_name="stock_profile_embeddings",
                profile_type="both",
                limit=limit * 3,
            )
            codes = [
                str(r.get("stock_code") or "").strip()
                for r in (all_rows or [])
                if str(r.get("stock_code") or "").strip() != target_code
            ]
            return codes[:limit]
        except Exception:
            return []

    # 向量相似度搜索
    try:
        result = await search_fn(
            collection_name="stock_profile_embeddings",
            query_embedding=target_embedding,
            top_k=limit + 1,
            profile_type="both",
        )
        items = list(result.get("items") or [])
        peers = [
            str(item.get("stock_code") or item.get("entity_id") or "").strip()
            for item in items
            if str(item.get("stock_code") or item.get("entity_id") or "").strip() != target_code
        ]
        return peers[:limit]
    except Exception as exc:
        logger.debug("_find_peer_stocks: vector search failed: %s", exc)
        return []


__all__ = ["validate_generalization"]
