"""Application layer lazy exports for migrated implementations."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORT_MAP: dict[str, tuple[str, str]] = {
    "BacktestFilter": (".backtest_filter", "BacktestFilter"),
    "FactoryCycleOutcome": (".cycle_runner", "FactoryCycleOutcome"),
    "FactoryCycleRunner": (".cycle_runner", "FactoryCycleRunner"),
    "FactoryRunContext": (".cycle_runner", "FactoryRunContext"),
    "FactoryTaskBoard": (".factory_task_board", "FactoryTaskBoard"),
    "FactoryV2Engine": (".v2_engine", "FactoryV2Engine"),
    "HypothesisLoweringCompiler": (".hypothesis_lowering_compiler", "HypothesisLoweringCompiler"),
    "DataCollector": (".collect", "DataCollector"),
    "Deduplicator": (".deduplicator", "Deduplicator"),
    "EliminationChecker": (".elimination", "EliminationChecker"),
    "FactorResearchBuilder": (".research.factor_research", "FactorResearchBuilder"),
    "GateResult": (".quality_gates", "GateResult"),
    "LOCAL_EVENT_ENGINE_NAME": (".event_engine", "LOCAL_EVENT_ENGINE_NAME"),
    "LocalEventDrivenResearchEngine": (".event_engine", "LocalEventDrivenResearchEngine"),
    "MarketOpportunityScanner": (".research.opportunity", "MarketOpportunityScanner"),
    "ResearchPlaneRunner": (".research.runner", "ResearchPlaneRunner"),
    "StockStrategyMatrixPlanner": (".research.matrix", "StockStrategyMatrixPlanner"),
    "StrategySpawner": (".research.spawner", "StrategySpawner"),
    "StrategyFactoryScheduler": (".factory_scheduler", "StrategyFactoryScheduler"),
    "StrategySubmitter": (".submitter", "StrategySubmitter"),
    "get_local_event_engine": (".event_engine", "get_local_event_engine"),
    "get_strategy_factory_package": (".factory_scheduler", "get_strategy_factory_package"),
    "get_runtime_strategy_factory_package": (".runtime", "get_strategy_factory_package"),
    "_call_optional_async": (".factory_scheduler", "_call_optional_async"),
    "_extract_event_context": (".utils", "_extract_event_context"),
    "_build_strategy_panels": (".panels", "_build_strategy_panels"),
    "_run_validation_report": (".panels", "_run_validation_report"),
    "_run_risk_report": (".panels", "_run_risk_report"),
    "gate_0_structural": (".quality_gates", "gate_0_structural"),
    "gate_1_fast_screen": (".quality_gates", "gate_1_fast_screen"),
    "build_pending_gate_3_report": (".quality_gates", "build_pending_gate_3_report"),
    "build_completed_gate_3_report": (".quality_gates", "build_completed_gate_3_report"),
    "run_gated_filter": (".quality_gates", "run_gated_filter"),
    "run_gated_submission_pipeline": (".quality_gates", "run_gated_submission_pipeline"),
    "build_legacy_gate_report": (".quality_gates", "build_legacy_gate_report"),
    "finalize_gate_report": (".quality_gates", "finalize_gate_report"),
    "PROVISIONAL_MIN_STATISTICAL_CHECKS_PASSED": (".quality_reporting", "PROVISIONAL_MIN_STATISTICAL_CHECKS_PASSED"),
    "quality_gate_reason_code": (".quality_reporting", "quality_gate_reason_code"),
    "normalize_quality_gate_result": (".quality_reporting", "normalize_quality_gate_result"),
    "is_factory_ai_prototype_strategy": (".quality_reporting", "is_factory_ai_prototype_strategy"),
    "has_only_statistical_gate_failures": (".quality_reporting", "has_only_statistical_gate_failures"),
    "safe_metric_value": (".quality_reporting", "safe_metric_value"),
    "maybe_grant_provisional_incubation": (".quality_reporting", "maybe_grant_provisional_incubation"),
    "build_quality_report": (".quality_reporting", "build_quality_report"),
    "run_submission_quality_gate": (".submission_gate", "run_submission_quality_gate"),
    "validate_precompile_candidate_contract": (".precompile_contract", "validate_precompile_candidate_contract"),
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
