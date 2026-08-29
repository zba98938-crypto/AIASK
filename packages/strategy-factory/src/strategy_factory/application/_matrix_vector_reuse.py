"""PR-S22 (策略工厂跑偏修复方案 P3 真复用)：vector reuse 索引与匹配。

设计要点：
- 在 matrix planner 主路径上，给每个候选 row 尝试根据"画像相似 + 已验证策略"
  注入一个 ``vector_reuse`` 提示。命中时 task 上会带：
  ``vector_reuse_strategy_id / params / similarity / source_code``。
- 索引来源：``db.list_strategies(status='listed')`` + 同 collection 的相似度查询。
- env 控制：``STRATEGY_FACTORY_VECTOR_REUSE_ENABLED`` 默认 0，避免无样本环境凑指标。
- 当 verified 样本不足 ``STRATEGY_FACTORY_VECTOR_REUSE_MIN_SAMPLES`` 时也不复用。

模块对外只暴露 ``VectorReuseService.build_from_db()`` 与
``service.match(row, family)``，调用方负责把命中信息写到 task。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class VectorReuseService:
    """以"已上线策略 + 画像相似"为基础做 task 级参数复用。"""

    def __init__(
        self,
        verified_index: list[dict[str, Any]],
        *,
        min_samples: int,
        min_similarity: float,
        topn: int,
    ) -> None:
        # verified_index 每条 = {strategy_id, strategy_type, params, target_codes, source}
        self._index_by_family: dict[str, list[dict[str, Any]]] = {}
        for item in list(verified_index or []):
            fam = str(item.get("strategy_type") or "").strip().lower()
            if not fam:
                continue
            self._index_by_family.setdefault(fam, []).append(dict(item))
        self._min_samples = max(1, int(min_samples or 1))
        self._min_similarity = max(0.0, min(float(min_similarity or 0.0), 1.0))
        self._topn = max(1, int(topn or 1))
        self._db = None
        self._lookup_count = 0
        self._hit_count = 0
        self._reuse_count = 0
        self._eligible_count = 0
        self._similarity_sum = 0.0

    @classmethod
    async def build_from_db(
        cls,
        db: Any,
        *,
        min_samples: int,
        min_similarity: float,
        topn: int,
        index_limit: int = 500,
    ) -> "VectorReuseService":
        """从 DB 构建 service：拉取已上线策略 + 关联其 target codes。"""

        verified: list[dict[str, Any]] = []
        list_strategies = getattr(db, "list_strategies", None)
        if not callable(list_strategies):
            return cls(verified, min_samples=min_samples, min_similarity=min_similarity, topn=topn)
        try:
            rows = await list_strategies(status="listed", limit=index_limit)
        except Exception as exc:
            logger.debug("VectorReuseService.build_from_db: list_strategies failed: %s", exc)
            rows = []
        for row in list(rows or []):
            payload = dict(row or {})
            params = dict(payload.get("params") or {})
            target_codes: list[str] = []
            for source in (
                params.get("target_symbols"),
                payload.get("target_symbols"),
            ):
                if isinstance(source, (list, tuple)):
                    for code in source:
                        token = str(code or "").strip()
                        if token and token not in target_codes:
                            target_codes.append(token)
            verified.append(
                {
                    "strategy_id": str(payload.get("id") or "").strip(),
                    "strategy_type": str(payload.get("strategy_type") or "").strip().lower(),
                    "params": params,
                    "target_codes": target_codes,
                    "source": "listed_strategy",
                }
            )
        service = cls(verified, min_samples=min_samples, min_similarity=min_similarity, topn=topn)
        service._db = db
        return service

    @property
    def index_count(self) -> int:
        return sum(len(v) for v in self._index_by_family.values())

    @property
    def lookup_count(self) -> int:
        return self._lookup_count

    @property
    def hit_count(self) -> int:
        return self._hit_count

    @property
    def reuse_count(self) -> int:
        return self._reuse_count

    @property
    def eligible_count(self) -> int:
        return self._eligible_count

    @property
    def avg_similarity(self) -> float:
        if self._reuse_count <= 0:
            return 0.0
        return round(self._similarity_sum / self._reuse_count, 4)

    def has_enough_samples(self) -> bool:
        return self.index_count >= self._min_samples

    async def match(
        self,
        row: dict[str, Any],
        family: str,
    ) -> Optional[dict[str, Any]]:
        """对一只股票 + family 尝试匹配 verified 索引中的复用项。

        返回结构（命中时）::

            {
                "strategy_id": "...",
                "params": {...},
                "similarity": 0.92,
                "source_code": "600519",
                "source": "listed_strategy",
            }

        PR-S22 性能优化：每只 row 的近邻只查一次，挂在
        ``row['_vector_reuse_neighbors']`` 上供同一 row 的多 family 共享。
        """

        self._lookup_count += 1
        normalized_family = str(family or "").strip().lower()
        family_pool = self._index_by_family.get(normalized_family) or []
        if not family_pool:
            return None
        if len(family_pool) < self._min_samples:
            return None

        target_code = str(row.get("code") or "").strip()
        if not target_code:
            return None

        if not self._db:
            return None
        list_profiles = getattr(self._db, "list_vector_profiles", None)
        search_collection = getattr(self._db, "search_vector_collection", None)
        if not callable(list_profiles) or not callable(search_collection):
            return None

        # 复用 row 上缓存的近邻，避免每个 family 重复查询
        cached = row.get("_vector_reuse_neighbors")
        if cached is not None:
            neighbors = list(cached)
        else:
            try:
                target_rows = await list_profiles(
                    collection_name="stock_profile_embeddings",
                    stock_code=target_code,
                    profile_type="both",
                    limit=1,
                )
            except Exception:
                row["_vector_reuse_neighbors"] = []
                return None
            if not target_rows:
                row["_vector_reuse_neighbors"] = []
                return None
            target_embedding = list(target_rows[0].get("embedding") or [])
            if not target_embedding:
                row["_vector_reuse_neighbors"] = []
                return None
            try:
                search_rows = await search_collection(
                    collection_name="stock_profile_embeddings",
                    query_embedding=target_embedding,
                    limit=self._topn + 1,
                    exclude_stock_code=target_code,
                )
            except TypeError:
                try:
                    search_rows = await search_collection(
                        collection_name="stock_profile_embeddings",
                        query_embedding=target_embedding,
                        limit=self._topn + 1,
                    )
                except Exception:
                    row["_vector_reuse_neighbors"] = []
                    return None
            except Exception:
                row["_vector_reuse_neighbors"] = []
                return None

            if isinstance(search_rows, dict):
                search_items = list(search_rows.get("items") or [])
            elif isinstance(search_rows, list):
                search_items = search_rows
            else:
                search_items = []

            neighbors = []
            for item in search_items:
                payload = dict(item or {})
                code = str(payload.get("stock_code") or "").strip()
                if not code or code == target_code:
                    continue
                sim = float(payload.get("similarity") or payload.get("score") or 0.0)
                if sim < self._min_similarity:
                    continue
                neighbors.append((code, sim))
                if len(neighbors) >= self._topn:
                    break
            row["_vector_reuse_neighbors"] = list(neighbors)

        if not neighbors:
            return None

        # 在 family_pool 中找到第一条 target_codes 与近邻交集的策略
        neighbor_codes = {code for code, _ in neighbors}
        self._eligible_count += 1
        for strat in family_pool:
            for source_code in strat.get("target_codes") or []:
                if source_code in neighbor_codes:
                    sim = next((s for c, s in neighbors if c == source_code), 0.0)
                    self._hit_count += 1
                    self._reuse_count += 1
                    self._similarity_sum += float(sim)
                    return {
                        "strategy_id": strat.get("strategy_id"),
                        "params": dict(strat.get("params") or {}),
                        "similarity": round(float(sim), 4),
                        "source_code": source_code,
                        "source": strat.get("source") or "listed_strategy",
                    }
        return None

    def to_summary(self) -> dict[str, Any]:
        return {
            "verified_strategy_index_count": self.index_count,
            "similar_profile_lookup_count": self.lookup_count,
            "similar_profile_hit_count": self.hit_count,
            "vector_reuse_eligible_count": self.eligible_count,
            "vector_reuse_count": self.reuse_count,
            "vector_reuse_avg_similarity": self.avg_similarity,
        }
