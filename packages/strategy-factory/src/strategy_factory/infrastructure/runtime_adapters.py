"""Host-neutral runtime adapter exports for Strategy Factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..api.contracts import (
    AutonomyGateway,
    FactorResearchGateway,
    IncubationGateway,
    RiskGateway,
    StrategyFactoryRepository,
    ValidationGateway,
    VectorSearchGateway,
)
from .mcp_adapters import (
    MCPAutonomyGatewayImpl,
    MCPFactorResearchGatewayImpl,
    MCPIncubationGatewayImpl,
    MCPRiskGatewayImpl,
    MCPStrategyFactoryRepositoryAdapter,
    MCPValidationGatewayImpl,
    MCPVectorSearchGatewayImpl,
    StrategyFactoryRepositoryAdapter,
    adapt_repository,
)


_RUNTIME_PROVIDER_ERROR = (
    "strategy-factory requires runtime providers for full cycle execution"
)


def _provider_error(name: str) -> RuntimeError:
    return RuntimeError(f"{_RUNTIME_PROVIDER_ERROR}; missing_provider={name}")


class _DeferredRuntimeGateway:
    """Delay host-provider errors until a gateway is actually exercised."""

    def __init__(self, provider_name: str):
        self._provider_name = provider_name
        self.runtime_provider_missing = provider_name

    def __getattr__(self, _name: str):
        raise _provider_error(self._provider_name)

    def __call__(self, *_args: Any, **_kwargs: Any):
        raise _provider_error(self._provider_name)

    async def __await_impl(self):
        raise _provider_error(self._provider_name)
        yield

    def __await__(self):
        return self.__await_impl().__await__()


def _build_optional_gateway(
    gateway_cls: type[Any],
    provider_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    try:
        return gateway_cls(*args, **kwargs)
    except RuntimeError as exc:
        if "missing_provider=" not in str(exc):
            raise
        return _DeferredRuntimeGateway(provider_name)


VectorSearchGatewayImpl = MCPVectorSearchGatewayImpl
AutonomyGatewayImpl = MCPAutonomyGatewayImpl
FactorResearchGatewayImpl = MCPFactorResearchGatewayImpl
IncubationGatewayImpl = MCPIncubationGatewayImpl
ValidationGatewayImpl = MCPValidationGatewayImpl
RiskGatewayImpl = MCPRiskGatewayImpl


@dataclass(frozen=True)
class RuntimeAdapters:
    """Typed bundle of host-provided runtime adapters."""

    repository: StrategyFactoryRepository
    vector_search: VectorSearchGateway
    autonomy: AutonomyGateway
    factor_research: FactorResearchGateway
    incubation: IncubationGateway
    validation: ValidationGateway
    risk: RiskGateway


MCPRuntimeAdapters = RuntimeAdapters


def build_runtime_adapters(
    db: Any,
    *,
    vector_engine: Any | None = None,
    autonomy_service: Any | None = None,
    factor_scheduler: Any | None = None,
    factor_research_builder: Any | None = None,
    incubation_service: Any | None = None,
    incubation_pipeline_service: Any | None = None,
    validation_runner=None,
    risk_runner=None,
) -> RuntimeAdapters:
    return RuntimeAdapters(
        repository=adapt_repository(db),
        vector_search=_build_optional_gateway(
            VectorSearchGatewayImpl,
            "vector_search",
            vector_engine,
        ),
        autonomy=_build_optional_gateway(
            AutonomyGatewayImpl,
            "autonomy_gateway",
            autonomy_service,
        ),
        factor_research=_build_optional_gateway(
            FactorResearchGatewayImpl,
            "factor_research_gateway",
            factor_scheduler,
            factor_research_builder,
        ),
        incubation=_build_optional_gateway(
            IncubationGatewayImpl,
            "incubation_gateway",
            incubation_service,
            incubation_pipeline_service,
        ),
        validation=ValidationGatewayImpl(validation_runner),
        risk=RiskGatewayImpl(risk_runner),
    )


build_mcp_runtime_adapters = build_runtime_adapters


__all__ = [
    # Canonical names (primary)
    "RuntimeAdapters",
    "build_runtime_adapters",
    "StrategyFactoryRepositoryAdapter",
    "adapt_repository",
    # Gateway implementation aliases (host-neutral)
    "AutonomyGatewayImpl",
    "FactorResearchGatewayImpl",
    "IncubationGatewayImpl",
    "RiskGatewayImpl",
    "ValidationGatewayImpl",
    "VectorSearchGatewayImpl",
    # MCP-prefixed gateway implementations (concrete)
    "MCPAutonomyGatewayImpl",
    "MCPFactorResearchGatewayImpl",
    "MCPIncubationGatewayImpl",
    "MCPRiskGatewayImpl",
    "MCPValidationGatewayImpl",
    "MCPVectorSearchGatewayImpl",
    # Compatibility aliases (deprecated)
    "MCPRuntimeAdapters",
    "MCPStrategyFactoryRepositoryAdapter",
    "build_mcp_runtime_adapters",
]
