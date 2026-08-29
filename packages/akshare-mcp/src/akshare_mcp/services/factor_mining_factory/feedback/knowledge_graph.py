"""因子知识图谱 — 记录因子间的关系和演化路径。

节点：因子候选（含验证结果）
边：derived_from / correlated_with / same_family / complementary / superseded_by
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FactorNode:
    """因子图节点。"""
    factor_id: str
    name: str
    family: str
    expression_dsl: str
    status: str = "active"  # active / decaying / retired
    grade: str = ""
    fitness: float = 0.0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "name": self.name,
            "family": self.family,
            "expression_dsl": self.expression_dsl,
            "status": self.status,
            "grade": self.grade,
            "fitness": self.fitness,
            "created_at": self.created_at,
        }


@dataclass
class FactorEdge:
    """因子图边。"""
    source_id: str
    target_id: str
    edge_type: str  # derived_from / correlated_with / same_family / complementary / superseded_by
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type,
            "weight": self.weight,
            "metadata": self.metadata,
        }


class FactorKnowledgeGraph:
    """因子知识图谱。

    用途：
    1. 追踪因子血缘关系（哪个因子是从哪个变异/精炼来的）
    2. 发现未探索区域（哪些家族/字段组合尚未充分搜索）
    3. 找到有潜力的父代因子（高 IC 但有改进空间）
    4. 避免重复搜索（已知失败的方向）
    """

    def __init__(self):
        self._nodes: dict[str, FactorNode] = {}
        self._edges: list[FactorEdge] = []
        self._adjacency: dict[str, list[FactorEdge]] = defaultdict(list)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def add_factor(
        self,
        factor_id: str,
        name: str,
        family: str,
        expression_dsl: str,
        *,
        status: str = "active",
        grade: str = "",
        fitness: float = 0.0,
        parent_id: str | None = None,
        derivation_type: str = "derived_from",
    ):
        """添加因子节点。"""
        node = FactorNode(
            factor_id=factor_id,
            name=name,
            family=family,
            expression_dsl=expression_dsl,
            status=status,
            grade=grade,
            fitness=fitness,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._nodes[factor_id] = node

        # 添加血缘边
        if parent_id and parent_id in self._nodes:
            self.add_edge(parent_id, factor_id, derivation_type)

        # 添加同家族边
        for existing_id, existing_node in self._nodes.items():
            if existing_id != factor_id and existing_node.family == family:
                self.add_edge(existing_id, factor_id, "same_family", weight=0.5)
                break  # 只连接一个同家族节点

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ):
        """添加边。"""
        edge = FactorEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            weight=weight,
            metadata=metadata or {},
        )
        self._edges.append(edge)
        self._adjacency[source_id].append(edge)

    def find_unexplored_families(self) -> list[dict[str, Any]]:
        """发现未充分探索的因子家族。"""
        family_counts: dict[str, int] = defaultdict(int)
        family_success: dict[str, int] = defaultdict(int)

        for node in self._nodes.values():
            family_counts[node.family] += 1
            if node.status == "active" and node.fitness > 1.0:
                family_success[node.family] += 1

        # 所有已知家族
        all_families = {"momentum", "volatility", "liquidity", "reversal", "custom",
                        "trend", "value", "quality", "growth", "sentiment", "divergence"}

        unexplored = []
        for family in all_families:
            count = family_counts.get(family, 0)
            success = family_success.get(family, 0)
            if count < 5:
                unexplored.append({
                    "family": family,
                    "explored_count": count,
                    "success_count": success,
                    "priority": "high" if count == 0 else "medium",
                })

        unexplored.sort(key=lambda x: x["explored_count"])
        return unexplored

    def find_promising_parents(self, top_k: int = 10) -> list[dict[str, Any]]:
        """找到最有潜力的父代因子（高 fitness + 少衍生）。"""
        parent_scores = []

        for factor_id, node in self._nodes.items():
            if node.status != "active":
                continue

            # 计算衍生数量
            derivation_count = sum(
                1 for edge in self._adjacency.get(factor_id, [])
                if edge.edge_type == "derived_from"
            )

            # 高 fitness + 少衍生 = 高潜力
            potential = node.fitness * (1.0 / (1.0 + derivation_count))
            parent_scores.append({
                "factor_id": factor_id,
                "name": node.name,
                "family": node.family,
                "expression_dsl": node.expression_dsl,
                "fitness": node.fitness,
                "derivation_count": derivation_count,
                "potential_score": potential,
            })

        parent_scores.sort(key=lambda x: -x["potential_score"])
        return parent_scores[:top_k]

    def get_lineage(self, factor_id: str) -> list[dict[str, Any]]:
        """获取因子的血缘链。"""
        lineage = []
        visited = set()
        current = factor_id

        while current and current not in visited:
            visited.add(current)
            node = self._nodes.get(current)
            if node:
                lineage.append(node.to_dict())

            # 找到父节点
            parent = None
            for edge in self._edges:
                if edge.target_id == current and edge.edge_type == "derived_from":
                    parent = edge.source_id
                    break
            current = parent

        return lineage

    def summary(self) -> dict[str, Any]:
        """图谱摘要。"""
        family_dist = defaultdict(int)
        status_dist = defaultdict(int)
        for node in self._nodes.values():
            family_dist[node.family] += 1
            status_dist[node.status] += 1

        edge_type_dist = defaultdict(int)
        for edge in self._edges:
            edge_type_dist[edge.edge_type] += 1

        return {
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "family_distribution": dict(family_dist),
            "status_distribution": dict(status_dist),
            "edge_type_distribution": dict(edge_type_dist),
            "unexplored_families": self.find_unexplored_families()[:5],
        }
