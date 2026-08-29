"""Runtime service registry used by Strategy Factory host adapters.

The Strategy Factory package owns orchestration and contracts. Concrete data,
vector, warmup, semantic, and lifecycle services are supplied by a host process
through ``configure_runtime_services``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from aiask_quant_core.backtest import BacktestEngine, StrategyRegistry
from aiask_quant_core.data_pipeline import normalize_klines
from aiask_quant_core.strategy_dsl import compile_strategy_blueprint


_DEFAULT_AUTONOMY_PHASE_ORDER = (
    "prepared",
    "generating",
    "reviewing",
    "recording",
    "submitting",
    "completed",
)

_RUNTIME_PROVIDER_ERROR = (
    "strategy-factory requires runtime providers for full cycle execution"
)

_PROVIDERS: dict[str, Any] = {}


def configure_runtime_services(**providers: Any) -> None:
    """Register host-provided runtime services.

    Passing ``None`` removes a provider. The registry intentionally stores
    opaque callables/objects so the package does not depend on host modules.
    """

    for name, provider in providers.items():
        if provider is None:
            _PROVIDERS.pop(name, None)
        else:
            _PROVIDERS[name] = provider


def clear_runtime_services() -> None:
    _PROVIDERS.clear()


def get_registered_runtime_provider_names() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def has_runtime_provider(name: str) -> bool:
    return str(name or "").strip() in _PROVIDERS


def missing_runtime_providers(required: tuple[str, ...] | list[str]) -> list[str]:
    missing: list[str] = []
    for raw_name in required:
        name = str(raw_name or "").strip()
        if name and name not in _PROVIDERS:
            missing.append(name)
    return missing


def _provider_error(name: str) -> RuntimeError:
    return RuntimeError(f"{_RUNTIME_PROVIDER_ERROR}; missing_provider={name}")


def _required(name: str, *, call: bool = False) -> Any:
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise _provider_error(name)
    if call and callable(provider):
        return provider()
    return provider


def _optional(name: str, default: Any = None, *, call: bool = False) -> Any:
    provider = _PROVIDERS.get(name)
    if provider is None:
        return default
    if call and callable(provider):
        return provider()
    return provider


def get_backtest_engine_class():
    return BacktestEngine


def get_strategy_registry():
    return StrategyRegistry


def get_normalize_klines():
    return normalize_klines


def get_risk_model_class():
    from aiask_quant_core.risk_model import RiskModel

    return RiskModel


def get_validation_runtime():
    import aiask_quant_core.validation as validation_runtime

    return SimpleNamespace(
        FactorValidationPipeline=validation_runtime.FactorValidationPipeline,
        PurgedKFoldCV=validation_runtime.PurgedKFoldCV,
        WalkForwardValidator=validation_runtime.WalkForwardValidator,
        bootstrap_ic_ci=validation_runtime.bootstrap_ic_ci,
        deflated_sharpe_ratio=validation_runtime.deflated_sharpe_ratio,
        probability_of_backtest_overfitting=validation_runtime.probability_of_backtest_overfitting,
        white_reality_check=validation_runtime.white_reality_check,
        hansen_spa_test=validation_runtime.hansen_spa_test,
    )


def get_factor_scheduler_singleton():
    return _required("factor_scheduler", call=True)


def get_factor_mining_factory():
    return _required("factor_mining_factory", call=True)


def get_factor_mining_support_factory():
    return _required("factor_mining_support_factory")


def get_factor_pool_gateway():
    return _required("factor_pool_gateway", call=True)


def get_incubation_runtime_factory():
    return _required("incubation_runtime_factory")


def get_incubation_runtime_support_factory():
    return _required("incubation_runtime_support_factory")


def get_signal_tracker_runtime_factory():
    return _required("signal_tracker_runtime_factory")


def get_signal_tracker_runtime_support_factory():
    return _required("signal_tracker_runtime_support_factory")


def get_market_event_ingest_runner():
    return _required("market_event_ingest_runner")


def get_market_event_ingest_support_factory():
    return _required("market_event_ingest_support_factory")


def get_quant_manager_callable():
    return _required("quant_manager_callable")


def get_runtime_warmup_runner():
    return _required("runtime_warmup_runner")


def get_strategy_promotion_pipeline_service():
    return _required("strategy_promotion_pipeline_service", call=True)


def get_strategy_runtime_control_service():
    return _required("strategy_runtime_control_service", call=True)


def get_strategy_runtime_risk_service():
    return _optional("strategy_runtime_risk_service_factory", call=True)


class _NullSentimentAnalyzer:
    def calculate_fear_greed_index(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "available": False,
            "fear_greed_index": 50,
            "level": "neutral",
            "reason": "sentiment_provider_missing",
        }


class _NullFinancialSemanticService:
    async def analyze_documents(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "available": False,
            "provider": None,
            "score": 0.0,
            "sentiment": "neutral",
            "fallback_reason": "semantic_provider_missing",
        }


def get_strategy_lifecycle_shared_runtime():
    runtime = _optional("strategy_lifecycle_shared_runtime")
    if runtime is not None:
        return runtime
    return SimpleNamespace(build_incubation_overview=None)


def get_event_context_builder():
    return _required("event_context_builder")


def get_sentiment_analyzer():
    return _optional("sentiment_analyzer", _NullSentimentAnalyzer())


def get_financial_semantic_service_factory():
    return _optional("financial_semantic_service_factory", lambda: _NullFinancialSemanticService())


def get_db_provider():
    return _required("db_provider")


async def _empty_index_kline_provider(index_code: str, period: str = "daily", limit: int = 60) -> dict[str, Any]:
    return {
        "success": False,
        "data": [],
        "source": "runtime_provider",
        "degraded": True,
        "message": "index kline provider missing",
        "index_code": str(index_code or ""),
        "period": str(period or "daily"),
        "limit": max(1, int(limit or 60)),
    }


def get_index_kline_provider():
    return _optional("index_kline_provider", _empty_index_kline_provider)


def get_strategy_dsl_compiler():
    return _optional("strategy_dsl_compiler", compile_strategy_blueprint)


def get_strategy_vector_platform_factory():
    return _required("strategy_vector_platform_factory")


def get_strategy_vector_search_engine_builder():
    return _optional("strategy_vector_search_engine_builder")


def get_strategy_autonomy_service():
    return _optional("strategy_autonomy_service_factory", call=True)


def get_strategy_incubation_service():
    return _optional("strategy_incubation_service_factory", call=True)


def get_strategy_incubation_pipeline_service():
    return _optional("strategy_incubation_pipeline_service_factory", call=True)


def get_strategy_vector_governance_service():
    return _optional("strategy_vector_governance_service_factory", call=True)


def get_strategy_domain_projection_service():
    return _optional("strategy_domain_projection_service_factory", call=True)


def get_strategy_lifecycle_scan_runner():
    return _optional("strategy_lifecycle_scan_runner")


def get_validation_runner():
    return _optional("validation_runner")


def get_risk_runner():
    return _optional("risk_runner")


def _summarize_autonomy_lifecycle_fallback(lifecycle: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(lifecycle or {})
    return {
        "state": payload.get("state"),
        "current_phase": payload.get("current_phase"),
        "failed_phase": payload.get("failed_phase"),
        "terminal_phase": payload.get("terminal_phase"),
        "phase_status_counts": dict(payload.get("phase_status_counts") or {}),
        "completed_phase_count": int(payload.get("completed_phase_count") or 0),
        "event_count": int(payload.get("event_count") or len(payload.get("events") or [])),
        "phase_order": list(payload.get("phase_order") or _DEFAULT_AUTONOMY_PHASE_ORDER),
    }


def get_autonomy_lifecycle_runtime():
    runtime = _optional("autonomy_lifecycle_runtime")
    if runtime is not None:
        return runtime
    return SimpleNamespace(
        AUTONOMY_PHASE_ORDER=_DEFAULT_AUTONOMY_PHASE_ORDER,
        summarize_autonomy_lifecycle=_summarize_autonomy_lifecycle_fallback,
    )


def build_strategy_vector_profile(db: Any, strategy: dict[str, Any]):
    builder = _optional("strategy_vector_profile_builder")
    if builder is None:
        return {
            "available": False,
            "strategy_id": dict(strategy or {}).get("id"),
            "reason": "vector_profile_provider_missing",
        }
    return builder(db, strategy)


def get_execution_audit_snapshot_builder():
    builder = _optional("execution_audit_snapshot_builder")
    if builder is not None:
        return builder

    async def _fallback(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "available": False,
            "reason": "execution_audit_provider_missing",
        }

    return _fallback


def get_closure_review_builder():
    builder = _optional("closure_review_builder")
    if builder is not None:
        return builder

    async def _fallback(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "available": False,
            "reason": "closure_review_provider_missing",
        }

    return _fallback


def get_strategy_llm_provider_loader():
    loader = _optional("strategy_llm_provider_loader")
    if loader is not None:
        return loader

    class _DisabledProvider:
        config = SimpleNamespace(connect_timeout_sec=0.0)

        def is_enabled(self) -> bool:
            return False

    return lambda: _DisabledProvider()


__all__ = [
    "build_strategy_vector_profile",
    "clear_runtime_services",
    "configure_runtime_services",
    "get_autonomy_lifecycle_runtime",
    "get_backtest_engine_class",
    "get_closure_review_builder",
    "get_db_provider",
    "get_event_context_builder",
    "get_execution_audit_snapshot_builder",
    "get_factor_mining_factory",
    "get_factor_mining_support_factory",
    "get_factor_pool_gateway",
    "get_factor_scheduler_singleton",
    "get_financial_semantic_service_factory",
    "get_index_kline_provider",
    "get_incubation_runtime_factory",
    "get_incubation_runtime_support_factory",
    "get_market_event_ingest_runner",
    "get_market_event_ingest_support_factory",
    "get_normalize_klines",
    "get_quant_manager_callable",
    "get_risk_model_class",
    "get_runtime_warmup_runner",
    "get_sentiment_analyzer",
    "get_signal_tracker_runtime_factory",
    "get_signal_tracker_runtime_support_factory",
    "get_strategy_dsl_compiler",
    "get_strategy_autonomy_service",
    "get_strategy_incubation_pipeline_service",
    "get_strategy_incubation_service",
    "get_strategy_lifecycle_scan_runner",
    "get_strategy_lifecycle_shared_runtime",
    "get_strategy_llm_provider_loader",
    "get_strategy_promotion_pipeline_service",
    "get_strategy_registry",
    "get_strategy_runtime_risk_service",
    "get_strategy_runtime_control_service",
    "get_strategy_domain_projection_service",
    "get_strategy_vector_governance_service",
    "get_strategy_vector_platform_factory",
    "get_strategy_vector_search_engine_builder",
    "get_validation_runtime",
    "get_validation_runner",
    "get_risk_runner",
]
