"""Strategy Factory public facade package."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_FACADE_EXPORTS = {
    "BACKTEST_AI_PROTOTYPE_THRESHOLDS",
    "CATEGORY_MINIMUMS",
    "DataCollector",
    "DEPRECATION_THRESHOLDS",
    "FactorResearchBuilder",
    "LOCAL_EVENT_ENGINE_NAME",
    "LLM_FAN_OUT_COUNT",
    "LocalEventDrivenResearchEngine",
    "MarketOpportunityScanner",
    "PIPELINE_MODE",
    "PIPELINE_STAGE_MAX_TOKENS",
    "PIPELINE_STAGE_TEMPERATURE",
    "PIPELINE_STAGE_TIMEOUTS",
    "PIPELINE_STAGE_TIMEOUT_SEC",
    "PROMOTION_THRESHOLDS",
    "PROVISIONAL_PASS_THRESHOLDS",
    "QUALITY_GATE_THRESHOLDS",
    "RISK_REPORT_THRESHOLDS",
    "StrategyFactoryScheduler",
    "StrategySpawner",
    "BacktestFilter",
    "Deduplicator",
    "StrategySubmitter",
    "EliminationChecker",
    "NormalizedEvent",
    "propagate_event_to_themes",
    "resolve_target_basket",
    "ThemeExposureBuilder",
    "seed_default_theme_graph",
    "generate_tasks_from_active_events",
    "ThemeResponseRegression",
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
    "STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED",
    "STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED",
    "STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED",
    "STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED",
    "STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED",
    "STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED",
    "STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED",
    "STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED",
    "STRATEGY_FACTORY_FEEDBACK_V2_ENABLED",
    "STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_ENABLED",
    "STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_MIN_N",
    "STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE",
}

__all__ = sorted(_FACADE_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _FACADE_EXPORTS:
        raise AttributeError(name)
    facade = import_module(".api.facade", __name__)
    return getattr(facade, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | _FACADE_EXPORTS)
