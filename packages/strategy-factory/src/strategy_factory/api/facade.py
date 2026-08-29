"""Stable public facade for the strategy_factory migration."""

from __future__ import annotations

import inspect
from importlib import import_module
from typing import Any

from ..application.runtime import get_strategy_factory_package as _runtime_get_strategy_factory_package
from ..domain.constants import (
    AUTONOMY_CANDIDATES_PER_TASK,
    AUTONOMY_MAX_RESEARCH_TASKS,
    AUTONOMY_STARTUP_DELAY_SEC,
    AUTONOMY_TASK_HARD_CAP,
    BACKTEST_AI_PROTOTYPE_THRESHOLDS,
    BACKTEST_DEFAULT_THRESHOLDS,
    BACKTEST_TYPE_THRESHOLDS,
    CATEGORY_MINIMUMS,
    DEPRECATION_THRESHOLDS,
    FACTORY_DAILY_RUN_TIME,
    FACTORY_EVENT_RUNTIME_MODE,
    FACTORY_FACTOR_AUTO_REFRESH,
    FACTORY_FACTOR_REFRESH_TIMEOUT_SEC,
    FACTORY_ERROR_BACKOFF_SEC,
    FACTORY_MARKET_HOURS_INTERVAL_SEC,
    FACTORY_MAX_DAILY_RUNS,
    FACTORY_OFF_HOURS_INTERVAL_SEC,
    FACTORY_READINESS_HARD_BLOCK,
    FACTORY_READINESS_MIN_COMPLETION_RATIO,
    FACTORY_READINESS_MIN_SCORE,
    STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED,
    STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED,
    STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED,
    STRATEGY_FACTORY_FEEDBACK_V2_ENABLED,
    STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED,
    STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED,
    STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_ENABLED,
    STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_MIN_N,
    STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED,
    STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED,
    STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE,
    STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED,
    FACTORY_PRE_GATE_ENABLED,
    FACTORY_RUNTIME_ENABLED,
    FACTORY_SCHEDULE_MODE,
    FACTORY_THROUGHPUT_TARGET_CANDIDATES_PER_HOUR,
    FACTORY_THROUGHPUT_TARGET_GATE3_PER_HOUR,
    LLM_FAN_OUT_COUNT,
    PIPELINE_MODE,
    PIPELINE_STAGE_MAX_TOKENS,
    PIPELINE_STAGE_TEMPERATURE,
    PIPELINE_STAGE_TIMEOUTS,
    PIPELINE_STAGE_TIMEOUT_SEC,
    PROMOTION_THRESHOLDS,
    PROVISIONAL_PASS_THRESHOLDS,
    QUALITY_GATE_THRESHOLDS,
    REPRESENTATIVE_STOCKS,
    RISK_REPORT_THRESHOLDS,
    SPAWNER_EVENT_SOURCE_BASE_CAP,
    SPAWNER_EVENT_SOURCE_SUPPLEMENTAL_BONUS,
    SPAWNER_EVENT_FILL_BUDGET_MAX,
    SPAWNER_FILL_BUDGET_MAX,
    SPAWNER_TARGET_TOTAL,
    STOCK_STRATEGY_MATRIX_ENABLED,
    STOCK_STRATEGY_MATRIX_BATCH_SIZE,
    STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY,
    STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK,
    STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK,
    STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN,
    STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN,
    STOCK_STRATEGY_MATRIX_RUN_WINDOW,
    STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD,
    STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
    preferred_strategy_types_for_factor,
)

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "BacktestFilter": ("..application.backtest_filter", "BacktestFilter"),
    "DataCollector": ("..application.collect", "DataCollector"),
    "Deduplicator": ("..application.deduplicator", "Deduplicator"),
    "EliminationChecker": ("..application.elimination", "EliminationChecker"),
    "FactorResearchBuilder": ("..application.research.factor_research", "FactorResearchBuilder"),
    "LOCAL_EVENT_ENGINE_NAME": ("..application.event_engine", "LOCAL_EVENT_ENGINE_NAME"),
    "LocalEventDrivenResearchEngine": ("..application.event_engine", "LocalEventDrivenResearchEngine"),
    "MarketOpportunityScanner": ("..application.research.opportunity", "MarketOpportunityScanner"),
    "ResearchPlaneRunner": ("..application.research.runner", "ResearchPlaneRunner"),
    "StockStrategyMatrixPlanner": ("..application.research.matrix", "StockStrategyMatrixPlanner"),
    "StrategyFactoryScheduler": ("..application.factory_scheduler", "StrategyFactoryScheduler"),
    "StrategySpawner": ("..application.research.spawner", "StrategySpawner"),
    "StrategySubmitter": ("..application.submitter", "StrategySubmitter"),
    # Theme-graph / event-driven research public exports (so akshare-mcp does not
    # need to reach into strategy_factory.application.research.* internals; see
    # tests/test_factory_db_only_boundary.py boundary contract).
    "NormalizedEvent": ("..application.research.theme_graph", "NormalizedEvent"),
    "propagate_event_to_themes": ("..application.research.theme_graph", "propagate_event_to_themes"),
    "resolve_target_basket": ("..application.research.target_basket", "resolve_target_basket"),
    "ThemeExposureBuilder": ("..application.research.theme_exposure_builder", "ThemeExposureBuilder"),
    "seed_default_theme_graph": ("..application.research.theme_seed", "seed_default_theme_graph"),
    "generate_tasks_from_active_events": ("..application.research.event_task_generator", "generate_tasks_from_active_events"),
    "ThemeResponseRegression": ("..application.research.theme_response_regression", "ThemeResponseRegression"),
    "_call_optional_async": ("..application.runtime", "_call_optional_async"),
    "get_local_event_engine": ("..application.event_engine", "get_local_event_engine"),
}

__all__ = [
    "BACKTEST_AI_PROTOTYPE_THRESHOLDS",
    "CATEGORY_MINIMUMS",
    "DataCollector",
    "DEPRECATION_THRESHOLDS",
    "MarketOpportunityScanner",
    "ResearchPlaneRunner",
    "StockStrategyMatrixPlanner",
    "FactorResearchBuilder",
    "LLM_FAN_OUT_COUNT",
    "LOCAL_EVENT_ENGINE_NAME",
    "LocalEventDrivenResearchEngine",
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
]


def _filter_supported_kwargs(factory: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    if not kwargs:
        return {}
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return dict(kwargs)
    allowed = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {key: value for key, value in kwargs.items() if key in allowed}


def _resolve_export(name: str) -> Any:
    try:
        module_name, attr_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name, __package__)
    return getattr(module, attr_name)


def _get_local_scheduler_target() -> Any:
    try:
        package = _runtime_get_strategy_factory_package()
    except Exception:
        package = None
    target = getattr(package, "get_strategy_factory_scheduler", None) if package is not None else None
    if callable(target):
        return target
    return _resolve_export("StrategyFactoryScheduler")


def _build_scheduler(target: Any, kwargs: dict[str, Any]):
    filtered_kwargs = _filter_supported_kwargs(target, kwargs)
    return target(**filtered_kwargs) if filtered_kwargs else target()


def _reject_removed_legacy_scheduler_kwargs(kwargs: dict[str, Any]) -> None:
    if "prefer_legacy" in kwargs:
        raise TypeError(
            "prefer_legacy has been removed and legacy scheduler access is no longer available"
        )


def get_strategy_factory_scheduler(**kwargs):
    _reject_removed_legacy_scheduler_kwargs(kwargs)
    return _build_scheduler(_get_local_scheduler_target(), kwargs)


async def run_submission_quality_gate(*args, **kwargs):
    local_target = import_module("..application.submission_gate", __package__).run_submission_quality_gate
    return await local_target(*args, **kwargs)


async def call_optional_async(*args, **kwargs):
    target = _resolve_export("_call_optional_async")
    return await target(*args, **kwargs)


def build_strategy_panels(*args, **kwargs):
    local_target = import_module("..application.panels", __package__)._build_strategy_panels
    return local_target(*args, **kwargs)


def extract_event_context(*args, **kwargs):
    local_target = import_module("..application.utils", __package__)._extract_event_context
    return local_target(*args, **kwargs)


def auto_name(*args, **kwargs):
    local_target = import_module("..domain.naming", __package__)._auto_name
    return local_target(*args, **kwargs)


def get_factory_constants() -> dict[str, Any]:
    return {
        "REPRESENTATIVE_STOCKS": REPRESENTATIVE_STOCKS,
        "CATEGORY_MINIMUMS": CATEGORY_MINIMUMS,
        "BACKTEST_DEFAULT_THRESHOLDS": BACKTEST_DEFAULT_THRESHOLDS,
        "BACKTEST_AI_PROTOTYPE_THRESHOLDS": BACKTEST_AI_PROTOTYPE_THRESHOLDS,
        "PROVISIONAL_PASS_THRESHOLDS": PROVISIONAL_PASS_THRESHOLDS,
        "QUALITY_GATE_THRESHOLDS": QUALITY_GATE_THRESHOLDS,
        "RISK_REPORT_THRESHOLDS": RISK_REPORT_THRESHOLDS,
        "PROMOTION_THRESHOLDS": PROMOTION_THRESHOLDS,
        "DEPRECATION_THRESHOLDS": DEPRECATION_THRESHOLDS,
        "AUTONOMY_MAX_RESEARCH_TASKS": AUTONOMY_MAX_RESEARCH_TASKS,
        "AUTONOMY_CANDIDATES_PER_TASK": AUTONOMY_CANDIDATES_PER_TASK,
        "AUTONOMY_TASK_HARD_CAP": AUTONOMY_TASK_HARD_CAP,
        "AUTONOMY_STARTUP_DELAY_SEC": AUTONOMY_STARTUP_DELAY_SEC,
        "BACKTEST_TYPE_THRESHOLDS": BACKTEST_TYPE_THRESHOLDS,
        "FACTORY_SCHEDULE_MODE": FACTORY_SCHEDULE_MODE,
        "FACTORY_EVENT_RUNTIME_MODE": FACTORY_EVENT_RUNTIME_MODE,
        "FACTORY_RUNTIME_ENABLED": FACTORY_RUNTIME_ENABLED,
        "FACTORY_FACTOR_AUTO_REFRESH": FACTORY_FACTOR_AUTO_REFRESH,
        "FACTORY_FACTOR_REFRESH_TIMEOUT_SEC": FACTORY_FACTOR_REFRESH_TIMEOUT_SEC,
        "FACTORY_READINESS_HARD_BLOCK": FACTORY_READINESS_HARD_BLOCK,
        "FACTORY_READINESS_MIN_SCORE": FACTORY_READINESS_MIN_SCORE,
        "FACTORY_READINESS_MIN_COMPLETION_RATIO": FACTORY_READINESS_MIN_COMPLETION_RATIO,
        "STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED": STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED,
        "STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED": STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED,
        "STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED": STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED,
        "STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED": STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED,
        "STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED": STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED,
        "STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED": STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED,
        "STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED": STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED,
        "STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED": STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED,
        "STRATEGY_FACTORY_FEEDBACK_V2_ENABLED": STRATEGY_FACTORY_FEEDBACK_V2_ENABLED,
        "STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE": STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE,
        "FACTORY_PRE_GATE_ENABLED": FACTORY_PRE_GATE_ENABLED,
        "FACTORY_MARKET_HOURS_INTERVAL_SEC": FACTORY_MARKET_HOURS_INTERVAL_SEC,
        "FACTORY_OFF_HOURS_INTERVAL_SEC": FACTORY_OFF_HOURS_INTERVAL_SEC,
        "FACTORY_MAX_DAILY_RUNS": FACTORY_MAX_DAILY_RUNS,
        "FACTORY_ERROR_BACKOFF_SEC": FACTORY_ERROR_BACKOFF_SEC,
        "FACTORY_DAILY_RUN_TIME": FACTORY_DAILY_RUN_TIME,
        "FACTORY_THROUGHPUT_TARGET_CANDIDATES_PER_HOUR": FACTORY_THROUGHPUT_TARGET_CANDIDATES_PER_HOUR,
        "FACTORY_THROUGHPUT_TARGET_GATE3_PER_HOUR": FACTORY_THROUGHPUT_TARGET_GATE3_PER_HOUR,
        "LLM_FAN_OUT_COUNT": LLM_FAN_OUT_COUNT,
        "PIPELINE_MODE": PIPELINE_MODE,
        "PIPELINE_STAGE_TIMEOUTS": PIPELINE_STAGE_TIMEOUTS,
        "PIPELINE_STAGE_MAX_TOKENS": PIPELINE_STAGE_MAX_TOKENS,
        "PIPELINE_STAGE_TEMPERATURE": PIPELINE_STAGE_TEMPERATURE,
        "PIPELINE_STAGE_TIMEOUT_SEC": PIPELINE_STAGE_TIMEOUT_SEC,
        "SPAWNER_TARGET_TOTAL": SPAWNER_TARGET_TOTAL,
        "SPAWNER_FILL_BUDGET_MAX": SPAWNER_FILL_BUDGET_MAX,
        "SPAWNER_EVENT_FILL_BUDGET_MAX": SPAWNER_EVENT_FILL_BUDGET_MAX,
        "SPAWNER_EVENT_SOURCE_BASE_CAP": SPAWNER_EVENT_SOURCE_BASE_CAP,
        "SPAWNER_EVENT_SOURCE_SUPPLEMENTAL_BONUS": SPAWNER_EVENT_SOURCE_SUPPLEMENTAL_BONUS,
        "STOCK_STRATEGY_MATRIX_ENABLED": STOCK_STRATEGY_MATRIX_ENABLED,
        "STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT": STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
        "STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK": STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK,
        "STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN": STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN,
        "STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN": STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN,
        "STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK": STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK,
        "STOCK_STRATEGY_MATRIX_BATCH_SIZE": STOCK_STRATEGY_MATRIX_BATCH_SIZE,
        "STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY": STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY,
        "STOCK_STRATEGY_MATRIX_RUN_WINDOW": STOCK_STRATEGY_MATRIX_RUN_WINDOW,
        "STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD": STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD,
        "HIGH_CONFIDENCE_FEATURE_FLAGS": {
            "high_confidence_enabled": STRATEGY_FACTORY_HIGH_CONFIDENCE_ENABLED,
            "evidence_contract_enabled": STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED,
            "confidence_diagnostics_enabled": STRATEGY_FACTORY_CONFIDENCE_DIAGNOSTICS_ENABLED,
            "execution_audit_enabled": STRATEGY_FACTORY_EXECUTION_AUDIT_ENABLED,
            "quality_ui_v2_enabled": STRATEGY_FACTORY_QUALITY_UI_V2_ENABLED,
            "research_protocol_v2_enabled": STRATEGY_FACTORY_RESEARCH_PROTOCOL_V2_ENABLED,
            "gate_model_v2_enabled": STRATEGY_FACTORY_GATE_MODEL_V2_ENABLED,
            "trace_ledger_v2_enabled": STRATEGY_FACTORY_TRACE_LEDGER_V2_ENABLED,
            "feedback_v2_enabled": STRATEGY_FACTORY_FEEDBACK_V2_ENABLED,
            "spec_completeness_mode": STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE,
        },
    }


def __getattr__(name: str) -> Any:
    return _resolve_export(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
