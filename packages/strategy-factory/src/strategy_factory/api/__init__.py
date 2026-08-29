"""Public API for strategy_factory."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_CONTRACT_EXPORTS = {
    "StrategyFactoryRepository",
    "VectorSearchGateway",
    "FactoryBacktestAssumptions",
    "ResearchValidationContract",
    "AutonomyGateway",
    "FactorResearchGateway",
    "IncubationGateway",
    "ValidationGateway",
    "RiskGateway",
    "StrategyResearchContract",
    "StrategyTargetingPolicy",
    "StrategyPortfolioSpec",
    "StrategyExecutionAssumptions",
    "StrategyValidationProfile",
    "StrategySubmissionAudit",
}

_DTO_EXPORTS = {
    "FactoryRunDetailDTO",
    "FactoryRunSummaryDTO",
    "FactoryStatusDTO",
    "StageResultDTO",
    "normalize_run_result_to_detail",
    "normalize_run_result_to_summary",
}

_FACTORY_RUN_EXPORTS = {
    "FACTORY_RUN_CONTRACT_VERSION",
    "FACTORY_RUN_TRANSITIONS",
    "TERMINAL_FACTORY_RUN_STATES",
    "FactoryName",
    "FactoryRunContract",
    "FactoryRunState",
    "normalize_factory_name",
    "normalize_factory_run_state",
    "validate_factory_run_transition",
}

_FACADE_EXPORTS = {
    "BACKTEST_AI_PROTOTYPE_THRESHOLDS",
    "BacktestFilter",
    "CATEGORY_MINIMUMS",
    "DataCollector",
    "DEPRECATION_THRESHOLDS",
    "Deduplicator",
    "EliminationChecker",
    "FactorResearchBuilder",
    "generate_tasks_from_active_events",
    "LOCAL_EVENT_ENGINE_NAME",
    "LLM_FAN_OUT_COUNT",
    "LocalEventDrivenResearchEngine",
    "MarketOpportunityScanner",
    "NormalizedEvent",
    "PIPELINE_MODE",
    "PIPELINE_STAGE_MAX_TOKENS",
    "PIPELINE_STAGE_TEMPERATURE",
    "PIPELINE_STAGE_TIMEOUTS",
    "PIPELINE_STAGE_TIMEOUT_SEC",
    "PROMOTION_THRESHOLDS",
    "propagate_event_to_themes",
    "PROVISIONAL_PASS_THRESHOLDS",
    "QUALITY_GATE_THRESHOLDS",
    "resolve_target_basket",
    "RISK_REPORT_THRESHOLDS",
    "seed_default_theme_graph",
    "StrategyFactoryScheduler",
    "StrategySpawner",
    "StrategySubmitter",
    "ThemeExposureBuilder",
    "ThemeResponseRegression",
    "_call_optional_async",
    "auto_name",
    "build_strategy_panels",
    "call_optional_async",
    "extract_event_context",
    "get_factory_constants",
    "get_local_event_engine",
    "get_strategy_factory_scheduler",
    "preferred_strategy_types_for_factor",
    "run_submission_quality_gate",
}

__all__ = [
    "StrategyFactoryRepository",
    "VectorSearchGateway",
    "FactoryBacktestAssumptions",
    "ResearchValidationContract",
    "AutonomyGateway",
    "FactorResearchGateway",
    "IncubationGateway",
    "ValidationGateway",
    "RiskGateway",
    "StrategyResearchContract",
    "StrategyTargetingPolicy",
    "StrategyPortfolioSpec",
    "StrategyExecutionAssumptions",
    "StrategyValidationProfile",
    "StrategySubmissionAudit",
    "FactoryRunDetailDTO",
    "FactoryRunSummaryDTO",
    "FactoryStatusDTO",
    "StageResultDTO",
    "normalize_run_result_to_detail",
    "normalize_run_result_to_summary",
    "BACKTEST_AI_PROTOTYPE_THRESHOLDS",
    "CATEGORY_MINIMUMS",
    "DataCollector",
    "DEPRECATION_THRESHOLDS",
    "MarketOpportunityScanner",
    "FactorResearchBuilder",
    "generate_tasks_from_active_events",
    "LOCAL_EVENT_ENGINE_NAME",
    "LLM_FAN_OUT_COUNT",
    "LocalEventDrivenResearchEngine",
    "NormalizedEvent",
    "PIPELINE_MODE",
    "PIPELINE_STAGE_MAX_TOKENS",
    "PIPELINE_STAGE_TEMPERATURE",
    "PIPELINE_STAGE_TIMEOUTS",
    "PIPELINE_STAGE_TIMEOUT_SEC",
    "PROMOTION_THRESHOLDS",
    "propagate_event_to_themes",
    "PROVISIONAL_PASS_THRESHOLDS",
    "QUALITY_GATE_THRESHOLDS",
    "resolve_target_basket",
    "RISK_REPORT_THRESHOLDS",
    "seed_default_theme_graph",
    "StrategyFactoryScheduler",
    "StrategySpawner",
    "BacktestFilter",
    "Deduplicator",
    "StrategySubmitter",
    "ThemeExposureBuilder",
    "ThemeResponseRegression",
    "EliminationChecker",
    "_call_optional_async",
    "call_optional_async",
    "auto_name",
    "get_local_event_engine",
    "get_strategy_factory_scheduler",
    "preferred_strategy_types_for_factor",
    "run_submission_quality_gate",
    "build_strategy_panels",
    "extract_event_context",
    "get_factory_constants",
    *_FACTORY_RUN_EXPORTS,
]


def __getattr__(name: str) -> Any:
    if name in _CONTRACT_EXPORTS:
        module = import_module(".contracts", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    if name in _DTO_EXPORTS:
        module = import_module(".dto", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    if name in _FACTORY_RUN_EXPORTS:
        module = import_module(".factory_runs", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    if name not in _FACADE_EXPORTS:
        raise AttributeError(name)
    facade = import_module(".facade", __name__)
    value = getattr(facade, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(
        set(globals()) | _CONTRACT_EXPORTS | _DTO_EXPORTS | _FACTORY_RUN_EXPORTS | _FACADE_EXPORTS
    )
