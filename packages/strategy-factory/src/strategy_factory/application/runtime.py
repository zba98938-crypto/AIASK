"""策略工厂运行时共享工具。"""

from __future__ import annotations

import asyncio
import inspect
from functools import lru_cache
from importlib import import_module
from types import SimpleNamespace
from typing import Any

_local_factory_scheduler = None


def _load_local_symbol(module_name: str, attr_name: str) -> Any:
    module = import_module(module_name, __package__)
    return getattr(module, attr_name)


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


def _apply_scheduler_runtime_injections(scheduler: Any, kwargs: dict[str, Any]) -> Any:
    if not kwargs:
        return scheduler

    db_provider = kwargs.get("db_provider")
    if db_provider is not None:
        scheduler._db_provider = db_provider

    runtime_adapters = kwargs.get("runtime_adapters")
    if runtime_adapters is not None:
        scheduler._runtime_adapters = runtime_adapters
        for attr_name, adapter_name in (
            ("_vector_gateway", "vector_search"),
            ("_validation_gateway", "validation"),
            ("_risk_gateway", "risk"),
            ("_incubation_gateway", "incubation"),
            ("_autonomy_gateway", "autonomy"),
            ("_factor_research_gateway", "factor_research"),
        ):
            adapter = getattr(runtime_adapters, adapter_name, None)
            if adapter is not None:
                setattr(scheduler, attr_name, adapter)

    for key, attr_name in (
        ("vector_gateway", "_vector_gateway"),
        ("validation_gateway", "_validation_gateway"),
        ("risk_gateway", "_risk_gateway"),
        ("incubation_gateway", "_incubation_gateway"),
        ("autonomy_gateway", "_autonomy_gateway"),
        ("factor_research_gateway", "_factor_research_gateway"),
    ):
        value = kwargs.get(key)
        if value is not None:
            setattr(scheduler, attr_name, value)

    return scheduler


def _get_local_strategy_factory_scheduler(**kwargs):
    global _local_factory_scheduler
    if _local_factory_scheduler is None:
        scheduler_cls = _load_local_symbol(".factory_scheduler", "StrategyFactoryScheduler")
        filtered_kwargs = _filter_supported_kwargs(scheduler_cls, kwargs)
        _local_factory_scheduler = scheduler_cls(**filtered_kwargs) if filtered_kwargs else scheduler_cls()
    elif kwargs:
        _apply_scheduler_runtime_injections(_local_factory_scheduler, kwargs)
    return _local_factory_scheduler


@lru_cache(maxsize=1)
def _build_local_runtime_view():
    from ..infrastructure.mcp_services import get_backtest_engine_class

    StrategySpawner = _load_local_symbol(".research.spawner", "StrategySpawner")
    BacktestFilter = _load_local_symbol(".backtest_filter", "BacktestFilter")
    DataCollector = _load_local_symbol(".collect", "DataCollector")
    Deduplicator = _load_local_symbol(".deduplicator", "Deduplicator")
    EliminationChecker = _load_local_symbol(".elimination", "EliminationChecker")
    LocalEventDrivenResearchEngine = _load_local_symbol(".event_engine", "LocalEventDrivenResearchEngine")
    get_local_event_engine = _load_local_symbol(".event_engine", "get_local_event_engine")
    FactorResearchBuilder = _load_local_symbol(".research.factor_research", "FactorResearchBuilder")
    StrategyFactoryScheduler = _load_local_symbol(".factory_scheduler", "StrategyFactoryScheduler")
    MarketOpportunityScanner = _load_local_symbol(".research.opportunity", "MarketOpportunityScanner")
    _build_strategy_panels = _load_local_symbol(".panels", "_build_strategy_panels")
    _run_risk_report = _load_local_symbol(".panels", "_run_risk_report")
    _run_validation_report = _load_local_symbol(".panels", "_run_validation_report")
    build_legacy_gate_report = _load_local_symbol(".quality_gates", "build_legacy_gate_report")
    finalize_gate_report = _load_local_symbol(".quality_gates", "finalize_gate_report")
    run_gated_filter = _load_local_symbol(".quality_gates", "run_gated_filter")
    run_gated_submission_pipeline = _load_local_symbol(".quality_gates", "run_gated_submission_pipeline")
    run_submission_quality_gate = _load_local_symbol(".submission_gate", "run_submission_quality_gate")
    StrategySubmitter = _load_local_symbol(".submitter", "StrategySubmitter")
    _extract_event_context = _load_local_symbol(".utils", "_extract_event_context")

    BacktestEngine = get_backtest_engine_class()

    return SimpleNamespace(
        asyncio=asyncio,
        BacktestEngine=BacktestEngine,
        DataCollector=DataCollector,
        MarketOpportunityScanner=MarketOpportunityScanner,
        StrategySpawner=StrategySpawner,
        BacktestFilter=BacktestFilter,
        Deduplicator=Deduplicator,
        StrategySubmitter=StrategySubmitter,
        EliminationChecker=EliminationChecker,
        LocalEventDrivenResearchEngine=LocalEventDrivenResearchEngine,
        get_local_event_engine=get_local_event_engine,
        FactorResearchBuilder=FactorResearchBuilder,
        StrategyFactoryScheduler=StrategyFactoryScheduler,
        _build_strategy_panels=_build_strategy_panels,
        _run_validation_report=_run_validation_report,
        _run_risk_report=_run_risk_report,
        run_gated_filter=run_gated_filter,
        run_gated_submission_pipeline=run_gated_submission_pipeline,
        build_legacy_gate_report=build_legacy_gate_report,
        finalize_gate_report=finalize_gate_report,
        get_strategy_factory_scheduler=_get_local_strategy_factory_scheduler,
        run_submission_quality_gate=run_submission_quality_gate,
        _extract_event_context=_extract_event_context,
    )


def get_strategy_factory_package():
    """返回本地策略工厂运行时视图。"""
    return _build_local_runtime_view()


async def _call_optional_async(target: Any, method_name: str, *args, default=None, **kwargs):
    method = getattr(target, method_name, None)
    if not callable(method):
        return default
    try:
        result = method(*args, **kwargs)
    except StopIteration:
        return default
    except StopAsyncIteration:
        return default
    if inspect.isawaitable(result):
        try:
            return await result
        except StopIteration:
            return default
        except StopAsyncIteration:
            return default
    return result


__all__ = [
    "get_strategy_factory_package",
    "_call_optional_async",
]
