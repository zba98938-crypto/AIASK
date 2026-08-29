"""AKShare-backed runtime providers for the Strategy Factory package.

This module is an in-process bridge, not an MCP tool surface. It lets callers
run ``strategy_factory`` directly while using AKShare MCP's SQLite storage and
local service implementations as explicit runtime providers.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable


def get_strategy_factory_db_provider() -> Callable[[], Any]:
    """Return the shared SQLite DB provider used by Strategy Factory runs."""

    from aiask_quant_core.storage import get_db

    return get_db


async def _get_index_kline_from_db(index_code: str, period: str = "daily", limit: int = 60) -> dict[str, Any]:
    if str(period or "daily").lower() not in {"daily", "day", "1d", "d"}:
        return {
            "success": False,
            "data": [],
            "source": "sqlite.kline_1d",
            "degraded": True,
            "message": "only daily index klines are synchronized to SQLite",
        }
    db = get_strategy_factory_db_provider()()
    rows = await db.get_index_klines(str(index_code or ""), limit=max(1, int(limit or 60)))
    return {
        "success": bool(rows),
        "data": rows,
        "source": "sqlite.kline_1d",
        "degraded": not bool(rows),
        "message": None if rows else "index klines missing in SQLite; run TDX sync",
    }


def _build_strategy_vector_profile(db: Any, strategy: dict[str, Any]):
    from ..services.vector_platform import get_strategy_vector_platform

    platform = get_strategy_vector_platform()
    return platform.build_strategy_profile(db, strategy)


def _strategy_lifecycle_shared_runtime() -> Any:
    from ..services import strategy_lifecycle_shared as module

    return SimpleNamespace(
        build_incubation_overview=getattr(module, "build_incubation_overview", None),
    )


def _autonomy_lifecycle_runtime() -> Any:
    from ..services import strategy_autonomy_lifecycle as module

    return SimpleNamespace(
        AUTONOMY_PHASE_ORDER=tuple(module.AUTONOMY_PHASE_ORDER),
        summarize_autonomy_lifecycle=module.summarize_autonomy_lifecycle,
    )


def _strategy_autonomy_service_runtime() -> Any:
    from ..services.strategy_autonomy import get_strategy_autonomy_service

    autonomy_service = get_strategy_autonomy_service()
    try:
        setattr(autonomy_service, "requires_repository_task_persistence", True)
    except Exception:
        pass
    return autonomy_service


def configure_akshare_storage_runtime_hooks() -> None:
    """Register AKShare host callbacks used by the shared storage layer."""

    from aiask_quant_core.storage.runtime_hooks import configure_storage_runtime_hooks

    from ..services import close_shared_runtime_clients
    from ..services.event_extraction import extract_events
    from ..services.sentiment import sentiment_analyzer
    from ..services.strategy_lifecycle_shared.confidence import evaluate_execution_audit_gate
    from ..services.strategy_lifecycle_shared.execution_audit_snapshot import (
        build_execution_audit_snapshot_payload,
        with_execution_audit_snapshot_metadata,
    )

    configure_storage_runtime_hooks(
        signal_evidence_builder=__import__(
            "strategy_factory.api.semantic_contract",
            fromlist=["build_signal_evidence_records"],
        ).build_signal_evidence_records,
        execution_audit_snapshot_builder=build_execution_audit_snapshot_payload,
        execution_audit_snapshot_metadata=with_execution_audit_snapshot_metadata,
        event_extractor=extract_events,
        headline_sentiment_classifier=sentiment_analyzer._classify_headline,
        text_embedding_service_factory=lambda: __import__(
            "akshare_mcp.services.text_embedding",
            fromlist=["get_strategy_text_embedding_service"],
        ).get_strategy_text_embedding_service(),
        rejected_kline_recorder=lambda **kwargs: __import__(
            "akshare_mcp.services.data_sync",
            fromlist=["data_sync_service"],
        ).data_sync_service.record_rejected_klines(**kwargs),
        execution_audit_gate_evaluator=evaluate_execution_audit_gate,
        cleanup_callbacks=(close_shared_runtime_clients,),
    )


def configure_strategy_factory_runtime_services() -> None:
    """Register AKShare service providers in Strategy Factory's host registry."""

    from strategy_factory.api.runtime import configure_runtime_services

    from ..services.decision_event_builder import build_event_context
    from ..services.factor_mining_factory import get_factor_mining_factory
    from ..services.factor_mining_factory.api import get_factor_pool_gateway
    from ..services.factor_scheduler import get_factor_scheduler
    from ..services.financial_semantic_service import get_financial_semantic_service
    from ..integration.strategy_factory_support import build_incubation_support
    from ..services.market_text_source_ingest import (
        get_market_event_ingest_support,
        run_market_text_source_ingest,
    )
    from ..services.promotion_pipeline import get_strategy_promotion_pipeline_service
    from ..services.runtime_risk import get_strategy_runtime_risk_service
    from ..services.runtime_control import get_strategy_runtime_control_service
    from ..services.sentiment import sentiment_analyzer
    from ..services.signal_tracker import get_signal_tracker
    from .signal_tracker import AKShareSignalTrackerProvider
    from ..services.strategy_dsl import compile_strategy_blueprint
    from ..services.strategy_llm_provider import get_strategy_llm_provider
    from ..services.incubation import get_strategy_incubation_service
    from ..services.incubation_pipeline import get_strategy_incubation_pipeline_service
    from ..services.strategy_lifecycle_shared import build_closure_review
    from ..services.strategy_lifecycle_shared.execution_audit_snapshot import (
        build_execution_audit_snapshot_payload,
    )
    from ..services.domain_projection import get_strategy_domain_projection_service
    from ..services.vector_governance import get_strategy_vector_governance_service
    from ..services.vector_platform import get_strategy_vector_platform
    from ..services.vector_search import VectorSearchEngine
    from ..tools.managers.data_sync_manager import run_runtime_data_warmup
    from ..tools.managers.quant_manager import quant_manager
    from ..tools.managers.strategy_manager import _lifecycle_scan

    configure_akshare_storage_runtime_hooks()

    configure_runtime_services(
        db_provider=get_strategy_factory_db_provider(),
        factor_scheduler=get_factor_scheduler,
        factor_mining_factory=get_factor_mining_factory,
        factor_mining_support_factory=get_factor_mining_factory,
        factor_pool_gateway=get_factor_pool_gateway,
        incubation_runtime_factory=build_incubation_support,
        incubation_runtime_support_factory=build_incubation_support,
        market_event_ingest_runner=run_market_text_source_ingest,
        market_event_ingest_support_factory=get_market_event_ingest_support,
        quant_manager_callable=quant_manager,
        runtime_warmup_runner=run_runtime_data_warmup,
        signal_tracker_runtime_factory=lambda **kwargs: AKShareSignalTrackerProvider(
            get_signal_tracker(**kwargs)
        ),
        signal_tracker_runtime_support_factory=lambda **kwargs: AKShareSignalTrackerProvider(
            get_signal_tracker(**kwargs)
        ),
        strategy_promotion_pipeline_service=get_strategy_promotion_pipeline_service,
        strategy_runtime_control_service=get_strategy_runtime_control_service,
        strategy_runtime_risk_service_factory=get_strategy_runtime_risk_service,
        strategy_lifecycle_shared_runtime=_strategy_lifecycle_shared_runtime(),
        event_context_builder=build_event_context,
        sentiment_analyzer=sentiment_analyzer,
        financial_semantic_service_factory=get_financial_semantic_service,
        index_kline_provider=_get_index_kline_from_db,
        strategy_dsl_compiler=compile_strategy_blueprint,
        strategy_vector_platform_factory=get_strategy_vector_platform,
        strategy_vector_search_engine_builder=lambda: VectorSearchEngine(
            backend="index",
            allow_fallback=True,
        ),
        strategy_autonomy_service_factory=_strategy_autonomy_service_runtime,
        strategy_incubation_service_factory=get_strategy_incubation_service,
        strategy_incubation_pipeline_service_factory=get_strategy_incubation_pipeline_service,
        autonomy_lifecycle_runtime=_autonomy_lifecycle_runtime(),
        strategy_vector_profile_builder=_build_strategy_vector_profile,
        strategy_vector_governance_service_factory=get_strategy_vector_governance_service,
        strategy_domain_projection_service_factory=get_strategy_domain_projection_service,
        strategy_lifecycle_scan_runner=_lifecycle_scan,
        execution_audit_snapshot_builder=build_execution_audit_snapshot_payload,
        closure_review_builder=build_closure_review,
        strategy_llm_provider_loader=get_strategy_llm_provider,
    )


def build_strategy_factory_runtime_adapters(db: Any | None = None) -> Any:
    """Compatibility shim for the canonical Strategy Factory runtime adapters."""

    from strategy_factory.runtime.default_bootstrap import build_default_runtime_adapters

    return build_default_runtime_adapters(db=db)


def build_strategy_factory_scheduler_kwargs(db: Any | None = None) -> dict[str, Any]:
    """Compatibility shim for canonical Strategy Factory scheduler kwargs."""

    from strategy_factory.runtime.default_bootstrap import build_default_scheduler_kwargs

    return build_default_scheduler_kwargs(db=db)


__all__ = [
    "configure_akshare_storage_runtime_hooks",
    "build_strategy_factory_runtime_adapters",
    "build_strategy_factory_scheduler_kwargs",
    "configure_strategy_factory_runtime_services",
    "get_strategy_factory_db_provider",
]
