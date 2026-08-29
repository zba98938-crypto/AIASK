"""Host-facing adapters for the strategy_factory contracts."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Mapping, Optional
from unittest.mock import AsyncMock, Mock

from ..api.contracts import (
    AutonomyGateway,
    FactorResearchGateway,
    IncubationGateway,
    RiskGateway,
    StrategyFactoryRepository,
    ValidationGateway,
    VectorSearchGateway,
)
from ..api.quality_reporting import run_risk_report, run_validation_report


_RUNTIME_PROVIDER_ERROR = (
    "strategy-factory requires runtime providers for full cycle execution"
)


def _provider_error(name: str) -> RuntimeError:
    return RuntimeError(f"{_RUNTIME_PROVIDER_ERROR}; missing_provider={name}")


async def _maybe_await(result: Any):
    if inspect.isawaitable(result):
        return await result
    return result


class StrategyFactoryRepositoryAdapter:
    """Thin wrapper that exposes an aggregate DB surface as a typed repository."""

    def __init__(self, db: Any):
        self._db = db

    @property
    def raw(self) -> Any:
        return self._db

    def acquire(self):
        acquire = getattr(self._db, "acquire", None)
        if not callable(acquire):
            raise AttributeError(f"{type(self._db).__name__} does not expose acquire()")
        return acquire()

    async def _call(self, method_name: str, *args: Any, **kwargs: Any):
        method = getattr(self._db, method_name)
        result = method(*args, **kwargs)
        return await _maybe_await(result)

    async def get_klines(self, code: str, limit: int = 500):
        return await self._call("get_klines", code, limit=limit)

    async def get_limit_up_stats(self):
        return await self._call("get_limit_up_stats")

    async def get_factor_ic_history(self, factor_name: str, horizon: str, limit: int):
        return await self._call("get_factor_ic_history", factor_name, horizon, limit)

    async def count_strategies_by_type(self, status: str):
        return await self._call("count_strategies_by_type", status)

    async def save_daily_snapshot(self, snapshot_date: Any, snapshot: Mapping[str, Any]):
        return await self._call("save_daily_snapshot", snapshot_date, snapshot)

    async def list_strategies(self, status: str, limit: int = 500):
        return await self._call("list_strategies", status, limit=limit)

    async def get_strategy(self, strategy_id: str):
        return await self._call("get_strategy", strategy_id)

    async def get_strategy_metrics(self, strategy_id: str):
        return await self._call("get_strategy_metrics", strategy_id)

    async def get_signal_stats(
        self,
        strategy_id: str,
        lookback_days: int | None = None,
        eps: float | None = None,
    ):
        kwargs: dict[str, Any] = {}
        if lookback_days is not None:
            kwargs["lookback_days"] = lookback_days
        if eps is not None:
            kwargs["eps"] = eps
        return await self._call("get_signal_stats", strategy_id, **kwargs)

    async def save_strategy(self, data: Mapping[str, Any]):
        return await self._call("save_strategy", data)

    async def save_strategy_quality_report(self, strategy_id: str, report_type: str, report: Mapping[str, Any]):
        return await self._call("save_strategy_quality_report", strategy_id, report_type, report)

    async def update_strategy_status(self, strategy_id: str, status: str, **kwargs: Any):
        return await self._call("update_strategy_status", strategy_id, status, **kwargs)

    async def save_strategy_lineage(
        self,
        strategy_id: str,
        parent_strategy_id: Optional[str],
        reason: str,
        snapshot: Mapping[str, Any],
    ):
        return await self._call("save_strategy_lineage", strategy_id, parent_strategy_id, reason, snapshot)

    async def save_strategy_metrics(self, strategy_id: str, period: str, payload: Mapping[str, Any]):
        return await self._call("save_strategy_metrics", strategy_id, period, payload)

    async def save_elimination_log(self, strategy_id: str, log_date: Any, red_flags: list[str], reason: str):
        return await self._call("save_elimination_log", strategy_id, log_date, red_flags, reason)

    async def get_strategy_generation_experiment(self, experiment_id: str):
        return await self._call("get_strategy_generation_experiment", experiment_id)

    async def save_strategy_generation_experiment(self, payload: Mapping[str, Any]):
        return await self._call("save_strategy_generation_experiment", payload)

    async def save_factory_task_evidence(self, payload: Mapping[str, Any]):
        return await self._call("save_factory_task_evidence", payload)

    async def save_strategy_candidate_evidence(self, payload: Mapping[str, Any]):
        return await self._call("save_strategy_candidate_evidence", payload)

    async def save_strategy_signal_evidence(self, payload: Mapping[str, Any]):
        return await self._call("save_strategy_signal_evidence", payload)

    async def save_strategy_task_run(self, payload: Mapping[str, Any]):
        return await self._call("save_strategy_task_run", payload)

    async def update_strategy_task_run(self, task_run_id: Any, **kwargs: Any):
        return await self._call("update_strategy_task_run", task_run_id, **kwargs)

    async def list_stock_universe(self, limit: int = 200, offset: int = 0):
        return await self._call("list_stock_universe", limit=limit, offset=offset)

    async def list_factory_event_clusters(self, status: Optional[str] = None, limit: int = 200):
        return await self._call("list_factory_event_clusters", status=status, limit=limit)

    async def save_factory_theme_definition(self, payload: Mapping[str, Any]):
        return await self._call("save_factory_theme_definition", payload)

    async def save_strategy_factory_run(self, results: Mapping[str, Any]):
        return await self._call("save_strategy_factory_run", results)

    async def list_strategy_factory_runs(self, limit: int = 20):
        return await self._call("list_strategy_factory_runs", limit=limit)

    async def get_strategy_factory_run(self, run_id: str):
        return await self._call("get_strategy_factory_run", run_id)

    async def get_latest_strategy_factory_run(self):
        return await self._call("get_latest_strategy_factory_run")

    async def save_strategy_factory_run_artifact(self, payload: Mapping[str, Any]):
        return await self._call("save_strategy_factory_run_artifact", payload)

    async def list_strategy_factory_run_artifacts(self, run_id: str):
        return await self._call("list_strategy_factory_run_artifacts", run_id)

    async def save_scheduler_state(self, payload: Mapping[str, Any]):
        return await self._call("save_scheduler_state", payload)

    async def load_scheduler_state(self, state_key: str = "strategy_factory_scheduler"):
        return await self._call("load_scheduler_state", state_key)

    async def create_strategy_factory_dispatch(self, payload: Mapping[str, Any]):
        return await self._call("create_strategy_factory_dispatch", payload)

    async def update_strategy_factory_dispatch(self, dispatch_id: str, **kwargs: Any):
        return await self._call("update_strategy_factory_dispatch", dispatch_id, **kwargs)

    async def get_strategy_factory_dispatch(self, dispatch_id: str):
        return await self._call("get_strategy_factory_dispatch", dispatch_id)

    async def list_strategy_factory_dispatches(self, status: Optional[str] = None, limit: int = 20):
        return await self._call("list_strategy_factory_dispatches", status=status, limit=limit)

    async def get_strategy_incubation_account(self, strategy_id: str):
        return await self._call("get_strategy_incubation_account", strategy_id)

    # --- Event-driven theme graph methods (PR-A) ---

    async def list_event_injections(self, status=None, source=None, limit=50):
        return await self._call("list_event_injections", status=status, source=source, limit=limit)

    async def list_theme_edges(self, source=None, target=None, is_active=True, limit=50):
        return await self._call("list_theme_edges", source=source, target=target, is_active=is_active, limit=limit)

    async def get_theme_node(self, theme_code: str):
        return await self._call("get_theme_node", theme_code)

    async def list_theme_nodes(self, is_active=True, limit=200):
        return await self._call("list_theme_nodes", is_active=is_active, limit=limit)

    async def list_theme_exposure(self, theme_code=None, min_exposure=0.3, limit=30):
        return await self._call("list_theme_exposure", theme_code=theme_code, min_exposure=min_exposure, limit=limit)

    async def upsert_event_task_lineage(self, payload: Mapping[str, Any]):
        return await self._call("upsert_event_task_lineage", payload)

    # --- Vector profiles (PR-S17 stock_profile_embeddings consumption) ---

    async def list_vector_profiles(
        self,
        *,
        collection_name: str | None = None,
        stock_codes: list[str] | None = None,
        stock_code: str | None = None,
        limit: int = 200,
    ):
        kwargs: dict[str, Any] = {"limit": limit}
        if collection_name is not None:
            kwargs["collection_name"] = collection_name
        if stock_codes is not None:
            kwargs["stock_codes"] = list(stock_codes)
        if stock_code is not None:
            kwargs["stock_code"] = stock_code
        return await self._call("list_vector_profiles", **kwargs)

    async def save_strategy_incubation_account(self, strategy_id: str, account_id: str, **kwargs: Any):
        return await self._call("save_strategy_incubation_account", strategy_id, account_id, **kwargs)


class MCPVectorSearchGatewayImpl:
    """Adapter over an injected vector search engine."""

    def __init__(self, engine: Any | None = None):
        if engine is None:
            raise _provider_error("vector_search")
        self._engine = engine

    @property
    def raw(self) -> Any:
        return self._engine

    @property
    def last_backend_used(self) -> str:
        return str(getattr(self._engine, "last_backend_used", "") or "")

    @property
    def last_meta(self) -> Mapping[str, Any]:
        return dict(getattr(self._engine, "last_meta", {}) or {})

    def find_similar_patterns(
        self,
        query_klines,
        candidate_klines_dict,
        top_k: int = 10,
        method: str = "price_volume",
        metric: str = "cosine",
        backend: Optional[str] = None,
        allow_fallback: Optional[bool] = None,
    ):
        return self._engine.find_similar_patterns(
            query_klines=query_klines,
            candidate_klines_dict=dict(candidate_klines_dict),
            top_k=top_k,
            method=method,
            metric=metric,
            backend=backend,
            allow_fallback=allow_fallback,
        )


class MCPAutonomyGatewayImpl:
    """Adapter over an injected strategy autonomy service."""

    def __init__(self, service: Any | None = None):
        if service is None:
            raise _provider_error("autonomy_gateway")
        self._service = service

    @property
    def raw(self) -> Any:
        return self._service

    @staticmethod
    def _filter_supported_kwargs(method: Any, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        if not kwargs:
            return {}
        try:
            signature = inspect.signature(method)
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

    @staticmethod
    def _has_concrete_repository_method(db: Any, method_name: str) -> bool:
        method = getattr(db, method_name, None)
        if method is None:
            return False
        if isinstance(method, Mock) and not isinstance(method, AsyncMock):
            return False
        return True

    def _requires_repository_task_persistence(self) -> bool:
        return bool(getattr(self._service, "requires_repository_task_persistence", False))

    async def generate_factory_candidates(
        self,
        db: StrategyFactoryRepository,
        snapshot: Mapping[str, Any],
        *,
        limit: int = 4,
        research_task: Optional[Mapping[str, Any]] = None,
        source: str = "",
    ):
        db = adapt_repository(db)
        if self._requires_repository_task_persistence():
            raw_db = getattr(db, "raw", db)
            required_methods = ("save_strategy_task_run", "update_strategy_task_run")
            missing_methods = [name for name in required_methods if not self._has_concrete_repository_method(raw_db, name)]
            if missing_methods:
                raise TypeError(
                    "autonomy repository is missing concrete persistence methods: "
                    + ", ".join(missing_methods)
                )
        method = self._service.generate_factory_candidates
        kwargs = self._filter_supported_kwargs(
            method,
            {
                "limit": limit,
                "research_task": research_task,
                "source": source,
            },
        )
        result = method(db, snapshot, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


class MCPFactorResearchGatewayImpl:
    """Adapter over an injected factor scheduler status API."""

    def __init__(self, scheduler: Any | None = None, builder: Any | None = None):
        if scheduler is None:
            raise _provider_error("factor_research_gateway")
        if builder is None:
            from ..application.factor_research import FactorResearchBuilder

            builder = FactorResearchBuilder
        self._scheduler = scheduler
        self._builder = builder

    @property
    def raw(self) -> Any:
        return self._scheduler

    @property
    def raw_builder(self) -> Any:
        return self._builder

    async def build_artifact(self, db: StrategyFactoryRepository, snapshot: Mapping[str, Any]):
        db = adapt_repository(db)
        builder = self._builder
        if builder is None:
            return {}
        build = getattr(builder, "build", None)
        if callable(build):
            return await _maybe_await(build(db, dict(snapshot or {})))
        if callable(builder):
            return await _maybe_await(builder(db, dict(snapshot or {})))
        return {}

    def status(self):
        return self._scheduler.status()

    async def refresh(self):
        refresh = getattr(self._scheduler, "run_once", None)
        if not callable(refresh):
            return {}
        return await _maybe_await(refresh())


class MCPIncubationGatewayImpl:
    """Adapter over injected incubation services."""

    def __init__(self, incubation_service: Any | None = None, pipeline_service: Any | None = None):
        if incubation_service is None:
            raise _provider_error("incubation_gateway")
        if pipeline_service is None:
            raise _provider_error("incubation_pipeline_gateway")
        self._incubation_service = incubation_service
        self._pipeline_service = pipeline_service

    @property
    def raw_incubation_service(self) -> Any:
        return self._incubation_service

    @property
    def raw_pipeline_service(self) -> Any:
        return self._pipeline_service

    async def ensure_account(
        self,
        db: StrategyFactoryRepository,
        strategy: Mapping[str, Any],
        *,
        source_run_id: Optional[Any] = None,
        stage: str = "warmup",
    ):
        db = adapt_repository(db)
        return await _maybe_await(self._incubation_service.ensure_account(
            db,
            strategy,
            source_run_id=source_run_id,
            stage=stage,
        ))

    async def run_pipeline(
        self,
        db: StrategyFactoryRepository,
        strategy: Mapping[str, Any],
        *,
        source: str = "strategy_factory_submit",
        auto_apply_review: bool = False,
    ):
        db = adapt_repository(db)
        return await _maybe_await(self._pipeline_service.run_strategy(
            db,
            strategy,
            source=source,
            auto_apply_review=auto_apply_review,
        ))

    async def submit(
        self,
        db: StrategyFactoryRepository,
        strategy: Mapping[str, Any],
        *,
        source_run_id: Optional[Any] = None,
        source: str = "strategy_factory_submit",
        auto_apply_review: bool = False,
        stage: str = "warmup",
    ):
        db = adapt_repository(db)
        binding = await self.ensure_account(db, strategy, source_run_id=source_run_id, stage=stage)
        pipeline = await self.run_pipeline(
            db,
            strategy,
            source=source,
            auto_apply_review=auto_apply_review,
        )
        return {"binding": binding, "pipeline": pipeline}


class MCPValidationGatewayImpl:
    """Adapter for the validation report runner used by the factory panels."""

    def __init__(self, runner=None):
        self._runner = runner or run_validation_report

    @property
    def raw(self) -> Any:
        return self._runner

    async def run_validation_report(self, strategy_type: str, params: Mapping[str, Any], db: Any):
        return await _maybe_await(self._runner(strategy_type, dict(params or {}), db))


class MCPRiskGatewayImpl:
    """Adapter for the risk report runner used by the factory panels."""

    def __init__(self, runner=None):
        self._runner = runner or run_risk_report

    @property
    def raw(self) -> Any:
        return self._runner

    async def run_risk_report(self, strategy_type: str, params: Mapping[str, Any], db: Any):
        return await _maybe_await(self._runner(strategy_type, dict(params or {}), db))


@dataclass(frozen=True)
class RuntimeAdapters:
    """Typed bundle of runtime adapters for incremental dependency injection.

    This is the canonical adapter type for strategy-factory runtime providers.
    Host integrations should inject concrete implementations through this interface.
    """

    repository: StrategyFactoryRepository
    vector_search: VectorSearchGateway
    autonomy: AutonomyGateway
    factor_research: FactorResearchGateway
    incubation: IncubationGateway
    validation: ValidationGateway
    risk: RiskGateway


def adapt_repository(db: Any) -> StrategyFactoryRepository:
    if isinstance(db, StrategyFactoryRepositoryAdapter):
        return db
    return StrategyFactoryRepositoryAdapter(db)


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
    """Build runtime adapters bundle - canonical factory (host-neutral).

    This is the primary entry point for constructing runtime adapter bundles.

    Returns:
        RuntimeAdapters instance with all injected dependencies
    """
    return RuntimeAdapters(
        repository=adapt_repository(db),
        vector_search=MCPVectorSearchGatewayImpl(vector_engine),
        autonomy=MCPAutonomyGatewayImpl(autonomy_service),
        factor_research=MCPFactorResearchGatewayImpl(factor_scheduler, factor_research_builder),
        incubation=MCPIncubationGatewayImpl(incubation_service, incubation_pipeline_service),
        validation=MCPValidationGatewayImpl(validation_runner),
        risk=MCPRiskGatewayImpl(risk_runner),
    )


# Compatibility aliases - these will be removed in a future version
MCPRuntimeAdapters = RuntimeAdapters
build_mcp_runtime_adapters = build_runtime_adapters
MCPStrategyFactoryRepositoryAdapter = StrategyFactoryRepositoryAdapter


__all__ = [
    # Canonical names (primary)
    "RuntimeAdapters",
    "build_runtime_adapters",
    "StrategyFactoryRepositoryAdapter",
    "adapt_repository",
    # Gateway implementations
    "MCPAutonomyGatewayImpl",
    "MCPFactorResearchGatewayImpl",
    "MCPIncubationGatewayImpl",
    "MCPRiskGatewayImpl",
    "MCPValidationGatewayImpl",
    "MCPVectorSearchGatewayImpl",
    # Compatibility aliases (deprecated)
    "MCPRuntimeAdapters",
    "build_mcp_runtime_adapters",
    "MCPStrategyFactoryRepositoryAdapter",
]
