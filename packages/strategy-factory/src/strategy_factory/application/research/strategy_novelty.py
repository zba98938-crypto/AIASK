"""PR-AI5: 历史策略相似度检索。

利用 strategy_artifact_text_embeddings（219 条，1536 维）检索历史相似策略，
避免 LLM 反复生成已被 Gate-3 拒绝的同类策略。

与已有 CandidateDedupDecision 的区别：
- CandidateDedupDecision 是候选之间的互相去重（同一批次内）
- 本模块是与历史已失败策略的去重（跨批次）
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.85


async def check_strategy_novelty(
    candidate: dict[str, Any],
    db: Any,
    embedding_service: Any = None,
) -> dict[str, Any]:
    """检索历史相似策略，避免重复无效策略。

    Args:
        candidate: 策略候选
        db: 数据库适配器（需支持 search_vector_collection）
        embedding_service: 文本嵌入服务（可选，不传则跳过）

    Returns:
        dict with novel (bool), reason, similar_count, etc.
    """
    search_fn = getattr(db, "search_vector_collection", None)
    if not callable(search_fn):
        return {"novel": True, "reason": "search_unavailable", "similar_count": 0}

    # 构建查询文本
    name = str(candidate.get("name") or "").strip()
    strategy_type = str(candidate.get("strategy_type") or "").strip()
    target_symbols = " ".join(str(s) for s in (candidate.get("target_symbols") or []))
    query_text = f"{name} {strategy_type} {target_symbols}".strip()

    if not query_text:
        return {"novel": True, "reason": "empty_query_text", "similar_count": 0}

    # 获取嵌入
    if embedding_service is None:
        return {"novel": True, "reason": "embedding_service_unavailable", "similar_count": 0}

    if not embedding_service or not hasattr(embedding_service, "embed"):
        return {"novel": True, "reason": "embedding_service_no_embed", "similar_count": 0}

    try:
        query_embedding = await embedding_service.embed(query_text)
        if not query_embedding or not isinstance(query_embedding, (list, tuple)):
            return {"novel": True, "reason": "embedding_failed", "similar_count": 0}
    except Exception as exc:
        logger.debug("strategy_novelty: embedding failed: %s", exc)
        return {"novel": True, "reason": f"embedding_error: {exc}", "similar_count": 0}

    # 向量检索
    try:
        result = await search_fn(
            collection_name="strategy_artifact_text_embeddings",
            query_embedding=list(query_embedding),
            top_k=5,
        )
    except Exception as exc:
        logger.debug("strategy_novelty: search failed: %s", exc)
        return {"novel": True, "reason": f"search_error: {exc}", "similar_count": 0}

    items = list(result.get("items") or [])
    similar = [
        item for item in items
        if float(item.get("similarity") or 0) > _SIMILARITY_THRESHOLD
    ]

    if not similar:
        return {"novel": True, "similar_count": len(items), "max_similarity": 0.0}

    # 检查相似策略的历史表现
    import json as _json
    for s in similar:
        try:
            meta = _json.loads(s.get("metadata") or "{}")
        except Exception:
            meta = {}
        validation_grade = str(meta.get("validation_grade") or "").strip().upper()
        status = str(meta.get("status") or "").strip().lower()
        if validation_grade == "D" or status in ("rejected", "eliminated"):
            return {
                "novel": False,
                "reason": (
                    f"与已拒绝策略 {s.get('entity_id', 'unknown')} "
                    f"相似度 {float(s.get('similarity', 0)):.2f} "
                    f"(grade={validation_grade}, status={status})"
                ),
                "similar_count": len(similar),
                "max_similarity": round(float(s.get("similarity") or 0), 4),
                "blocked_by": str(s.get("entity_id") or ""),
            }

    # 有相似但都不是失败的 — 允许通过但记录
    return {
        "novel": True,
        "similar_count": len(similar),
        "max_similarity": round(max(float(s.get("similarity") or 0) for s in similar), 4),
        "note": "similar_strategies_exist_but_not_rejected",
    }


__all__ = ["check_strategy_novelty"]
