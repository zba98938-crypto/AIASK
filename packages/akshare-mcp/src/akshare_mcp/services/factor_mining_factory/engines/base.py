"""搜索引擎基础协议与数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SearchBudget:
    """搜索预算分配。"""
    candidate_count: int = 10
    max_time_sec: float = 60.0
    max_iterations: int = 100


@dataclass
class EngineStatus:
    """引擎健康状态。"""
    engine_id: str
    engine_type: str
    enabled: bool = True
    ready: bool = True
    last_run_at: str | None = None
    last_error: str | None = None
    success_count: int = 0
    failure_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "engine_type": self.engine_type,
            "enabled": self.enabled,
            "ready": self.ready,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
        }


@dataclass
class FactorCandidate:
    """候选因子统一数据模型。"""
    name: str
    hypothesis: str = ""
    economic_hypothesis: str = ""
    family: str = "custom"
    factor_family: str = ""
    inputs: list[str] = field(default_factory=list)
    expression_dsl: str = ""
    expected_holding_period: int = 10
    expected_horizon: int = 10
    expected_regime: list[str] = field(default_factory=list)
    complexity_hint: str = "medium"
    novelty_rationale: str = ""
    generation_engine: str = ""
    blueprint_id: str = ""
    risk_exposure_hint: dict[str, Any] = field(default_factory=dict)
    quick_evidence: dict[str, Any] = field(default_factory=dict)
    generation_trace: dict[str, Any] = field(default_factory=dict)
    fitness: float = 0.0
    validation_result: dict[str, Any] | None = None

    def to_validation_dict(self) -> dict[str, Any]:
        """转换为验证流水线所需的 dict 格式。"""
        return {
            "name": self.name,
            "hypothesis": self.economic_hypothesis or self.hypothesis,
            "family": self.factor_family or self.family,
            "inputs": self.inputs,
            "expression_dsl": self.expression_dsl,
            "expected_holding_period": self.expected_holding_period or self.expected_horizon,
            "expected_regime": self.expected_regime,
            "complexity_hint": self.complexity_hint,
            "novelty_rationale": self.novelty_rationale,
            "economic_hypothesis": self.economic_hypothesis or self.hypothesis,
            "factor_family": self.factor_family or self.family,
            "expected_horizon": self.expected_horizon or self.expected_holding_period,
            "risk_exposure_hint": self.risk_exposure_hint,
            "blueprint_id": self.blueprint_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "hypothesis": self.hypothesis,
            "economic_hypothesis": self.economic_hypothesis,
            "family": self.family,
            "factor_family": self.factor_family or self.family,
            "inputs": self.inputs,
            "expression_dsl": self.expression_dsl,
            "expected_holding_period": self.expected_holding_period,
            "expected_horizon": self.expected_horizon or self.expected_holding_period,
            "expected_regime": self.expected_regime,
            "complexity_hint": self.complexity_hint,
            "novelty_rationale": self.novelty_rationale,
            "generation_engine": self.generation_engine,
            "blueprint_id": self.blueprint_id,
            "risk_exposure_hint": self.risk_exposure_hint,
            "quick_evidence": self.quick_evidence,
            "generation_trace": self.generation_trace,
            "fitness": self.fitness,
        }


@runtime_checkable
class SearchEngine(Protocol):
    """搜索引擎统一接口 — 所有引擎实现此协议。"""

    engine_id: str
    engine_type: str

    async def generate(
        self,
        context: Any,
        budget: SearchBudget,
    ) -> list[FactorCandidate]:
        """生成候选因子。"""
        ...

    def get_status(self) -> EngineStatus:
        """引擎健康状态。"""
        ...
