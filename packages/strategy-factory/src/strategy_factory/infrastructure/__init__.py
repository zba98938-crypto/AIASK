"""Lazy infrastructure exports for MCP-backed adapters and service loaders."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORT_MAP: dict[str, tuple[str, str]] = {
    "AutonomyGatewayImpl": (".runtime_adapters", "AutonomyGatewayImpl"),
    "FactorResearchGatewayImpl": (".runtime_adapters", "FactorResearchGatewayImpl"),
    "IncubationGatewayImpl": (".runtime_adapters", "IncubationGatewayImpl"),
    "MCPAutonomyGatewayImpl": (".runtime_adapters", "MCPAutonomyGatewayImpl"),
    "MCPFactorResearchGatewayImpl": (".runtime_adapters", "MCPFactorResearchGatewayImpl"),
    "MCPIncubationGatewayImpl": (".runtime_adapters", "MCPIncubationGatewayImpl"),
    "MCPRiskGatewayImpl": (".runtime_adapters", "MCPRiskGatewayImpl"),
    "MCPRuntimeAdapters": (".runtime_adapters", "MCPRuntimeAdapters"),
    "MCPStrategyFactoryRepositoryAdapter": (".runtime_adapters", "MCPStrategyFactoryRepositoryAdapter"),
    "MCPValidationGatewayImpl": (".runtime_adapters", "MCPValidationGatewayImpl"),
    "MCPVectorSearchGatewayImpl": (".runtime_adapters", "MCPVectorSearchGatewayImpl"),
    "RiskGatewayImpl": (".runtime_adapters", "RiskGatewayImpl"),
    "RuntimeAdapters": (".runtime_adapters", "RuntimeAdapters"),
    "adapt_repository": (".runtime_adapters", "adapt_repository"),
    "build_mcp_runtime_adapters": (".runtime_adapters", "build_mcp_runtime_adapters"),
    "build_runtime_adapters": (".runtime_adapters", "build_runtime_adapters"),
    "StrategyFactoryRepositoryAdapter": (".runtime_adapters", "StrategyFactoryRepositoryAdapter"),
    "ValidationGatewayImpl": (".runtime_adapters", "ValidationGatewayImpl"),
    "VectorSearchGatewayImpl": (".runtime_adapters", "VectorSearchGatewayImpl"),
    "build_strategy_vector_profile": (".runtime_services", "build_strategy_vector_profile"),
    "get_autonomy_lifecycle_runtime": (".runtime_services", "get_autonomy_lifecycle_runtime"),
    "get_backtest_engine_class": (".runtime_services", "get_backtest_engine_class"),
    "get_factor_scheduler_singleton": (".runtime_services", "get_factor_scheduler_singleton"),
    "get_normalize_klines": (".runtime_services", "get_normalize_klines"),
    "get_risk_model_class": (".runtime_services", "get_risk_model_class"),
    "get_sentiment_analyzer": (".runtime_services", "get_sentiment_analyzer"),
    "get_strategy_dsl_compiler": (".runtime_services", "get_strategy_dsl_compiler"),
    "get_strategy_registry": (".runtime_services", "get_strategy_registry"),
    "get_strategy_vector_platform_factory": (".runtime_services", "get_strategy_vector_platform_factory"),
    "get_validation_runtime": (".runtime_services", "get_validation_runtime"),
}

__all__ = list(_EXPORT_MAP)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORT_MAP[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name, __name__)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
