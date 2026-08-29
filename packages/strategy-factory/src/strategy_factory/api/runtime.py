"""Public runtime-provider API for host integrations."""

from __future__ import annotations

from ..application.runtime import _call_optional_async, get_strategy_factory_package
from ..application.runtime_boundary import (
    RuntimeBoundaryReport,
    validate_strategy_factory_runtime,
)
from ..infrastructure.runtime_adapters import (
    RuntimeAdapters,
    build_runtime_adapters,
    AutonomyGatewayImpl,
    FactorResearchGatewayImpl,
    IncubationGatewayImpl,
    RiskGatewayImpl,
    ValidationGatewayImpl,
    VectorSearchGatewayImpl,
    MCPAutonomyGatewayImpl,
    MCPFactorResearchGatewayImpl,
    MCPIncubationGatewayImpl,
    MCPRiskGatewayImpl,
    MCPRuntimeAdapters,
    MCPStrategyFactoryRepositoryAdapter,
    MCPValidationGatewayImpl,
    MCPVectorSearchGatewayImpl,
    StrategyFactoryRepositoryAdapter,
    adapt_repository,
    build_mcp_runtime_adapters,
)
from ..contracts.host_adapter import (
    DEFAULT_SIDE_EFFECT_POLICY,
    HOST_ADAPTER_PROTOCOL_VERSION,
    KNOWN_OPERATIONS,
    CapabilityReport,
    HostAdapter,
    McpManifest,
    SkillBundle,
    assert_policy_not_loosened,
    default_side_effect,
    missing_adapter_methods,
    verify_host_adapter,
)
from ..contracts.run_envelope import (
    PROTOCOL_VERSION,
    REQUIRED_ENVELOPE_FIELDS,
    RunEnvelope,
    envelope_from_runtime_result,
    new_run_id,
    normalize_status,
    utc_now_iso,
)
from ..domain.observation_lifecycle import (
    OBSERVATION_DECISIONS,
    ObservationLifecycleDecision,
    ObservationLifecyclePolicy,
)
from ..domain.promotion_gate import (
    PROMOTION_DSR_ENV,
    PromotionGate,
    PromotionGateVerdict,
    promotion_gate_enabled,
)
from ..domain.stage_status import (
    RUN_STATUSES,
    STAGE_STATUSES,
    build_run_summary,
    build_stage_result,
    normalize_stage_status,
    quality_status,
    resolve_run_status,
    summarize_stage_results,
)
from ..infrastructure.factory_repository import (
    FactoryRepository,
    FactoryRepositoryError,
    assert_factory_domain_ready,
    build_factory_repository,
    factory_domain_methods,
)
from ..infrastructure.factory_schema import (
    FACTORY_SCHEMA_NAMESPACE,
    factory_domain_tables,
    init_factory_schema,
    register_factory_schema,
    verify_factory_schema,
)
from ..infrastructure.mcp_services import (
    build_strategy_vector_profile,
    clear_runtime_services,
    configure_runtime_services,
    get_autonomy_lifecycle_runtime,
    get_backtest_engine_class,
    get_closure_review_builder,
    get_db_provider,
    get_event_context_builder,
    get_execution_audit_snapshot_builder,
    get_factor_mining_factory,
    get_factor_pool_gateway,
    get_factor_scheduler_singleton,
    get_financial_semantic_service_factory,
    get_index_kline_provider,
    get_normalize_klines,
    get_quant_manager_callable,
    get_risk_model_class,
    get_runtime_warmup_runner,
    get_sentiment_analyzer,
    get_strategy_dsl_compiler,
    get_strategy_lifecycle_shared_runtime,
    get_strategy_llm_provider_loader,
    get_strategy_promotion_pipeline_service,
    get_strategy_registry,
    get_strategy_runtime_control_service,
    get_strategy_vector_platform_factory,
    get_validation_runtime,
)


def build_scheduler_runtime_kwargs(db=None):
    """Build canonical scheduler kwargs from the registered runtime providers.

    This function bridges the old scheduler interface to the new runtime adapters.
    Used by default_bootstrap.py for backward compatibility.
    """
    from ..infrastructure.mcp_services import (
        get_strategy_autonomy_service,
        get_strategy_incubation_service,
        get_strategy_incubation_pipeline_service,
        get_factor_scheduler_singleton,
        get_validation_runner,
        get_risk_runner,
        get_strategy_vector_search_engine_builder,
    )

    db_provider = get_db_provider()
    resolved_db = db if db is not None else db_provider()

    vector_engine_builder = get_strategy_vector_search_engine_builder()
    vector_engine = vector_engine_builder() if callable(vector_engine_builder) else None

    return {
        "db_provider": db_provider,
        "runtime_adapters": build_runtime_adapters(
            resolved_db,
            vector_engine=vector_engine,
            autonomy_service=get_strategy_autonomy_service(),
            factor_scheduler=get_factor_scheduler_singleton(),
            incubation_service=get_strategy_incubation_service(),
            incubation_pipeline_service=get_strategy_incubation_pipeline_service(),
            validation_runner=get_validation_runner(),
            risk_runner=get_risk_runner(),
        ),
    }


def create_theme_response_regression():
    """Create the current theme-regression implementation.

    Resolve the class at call time so test/plugin reloads cannot leave the
    public facade holding a stale class object from an earlier module graph.
    """
    from ..application.research.theme_response_regression import ThemeResponseRegression

    return ThemeResponseRegression()

__all__ = [
    # Canonical names (primary)
    "RuntimeAdapters",
    "build_runtime_adapters",
    "StrategyFactoryRepositoryAdapter",
    # Factory-domain persistence facade (Phase 2 ownership)
    "FactoryRepository",
    "FactoryRepositoryError",
    "assert_factory_domain_ready",
    "build_factory_repository",
    "factory_domain_methods",
    # Factory-domain schema ownership (Phase 2)
    "FACTORY_SCHEMA_NAMESPACE",
    "factory_domain_tables",
    "init_factory_schema",
    "register_factory_schema",
    "verify_factory_schema",
    # Governed run envelope — re-exported so callers depend on the public
    # surface rather than reaching into strategy_factory.contracts directly.
    "PROTOCOL_VERSION",
    "REQUIRED_ENVELOPE_FIELDS",
    "RunEnvelope",
    "envelope_from_runtime_result",
    "new_run_id",
    "normalize_status",
    "utc_now_iso",
    # Host Adapter Contract (Phase 5)
    "DEFAULT_SIDE_EFFECT_POLICY",
    "HOST_ADAPTER_PROTOCOL_VERSION",
    "KNOWN_OPERATIONS",
    "CapabilityReport",
    "HostAdapter",
    "McpManifest",
    "SkillBundle",
    "assert_policy_not_loosened",
    "default_side_effect",
    "missing_adapter_methods",
    "verify_host_adapter",
    # Stage-status vocabulary and run-status resolution (Phase 3 ownership)
    "RUN_STATUSES",
    "STAGE_STATUSES",
    "build_run_summary",
    "build_stage_result",
    "normalize_stage_status",
    "quality_status",
    "resolve_run_status",
    "summarize_stage_results",
    # Observation lifecycle policy (Phase 3 ownership)
    "OBSERVATION_DECISIONS",
    "ObservationLifecycleDecision",
    "ObservationLifecyclePolicy",
    # Promotion gate decision (Phase 3 ownership)
    "PROMOTION_DSR_ENV",
    "PromotionGate",
    "PromotionGateVerdict",
    "promotion_gate_enabled",
    # Gateway implementation aliases
    "AutonomyGatewayImpl",
    "FactorResearchGatewayImpl",
    "IncubationGatewayImpl",
    "RiskGatewayImpl",
    "ValidationGatewayImpl",
    "VectorSearchGatewayImpl",
    # Runtime boundary
    "RuntimeBoundaryReport",
    "validate_strategy_factory_runtime",
    # Runtime services configuration
    "configure_runtime_services",
    "clear_runtime_services",
    "build_scheduler_runtime_kwargs",
    "create_theme_response_regression",
    # Runtime service getters
    "get_db_provider",
    "get_factor_scheduler_singleton",
    "get_factor_mining_factory",
    "get_factor_pool_gateway",
    "get_quant_manager_callable",
    "get_runtime_warmup_runner",
    "get_strategy_promotion_pipeline_service",
    "get_strategy_runtime_control_service",
    "get_strategy_lifecycle_shared_runtime",
    "get_event_context_builder",
    "get_sentiment_analyzer",
    "get_financial_semantic_service_factory",
    "get_index_kline_provider",
    "get_strategy_dsl_compiler",
    "get_strategy_vector_platform_factory",
    "get_autonomy_lifecycle_runtime",
    "get_execution_audit_snapshot_builder",
    "get_closure_review_builder",
    "get_strategy_llm_provider_loader",
    "get_backtest_engine_class",
    "get_risk_model_class",
    "get_normalize_klines",
    "get_validation_runtime",
    "get_strategy_registry",
    "get_strategy_factory_package",
    # Utilities
    "_call_optional_async",
    "adapt_repository",
    "build_strategy_vector_profile",
    # Compatibility aliases (deprecated)
    "MCPAutonomyGatewayImpl",
    "MCPFactorResearchGatewayImpl",
    "MCPIncubationGatewayImpl",
    "MCPRiskGatewayImpl",
    "MCPRuntimeAdapters",
    "MCPStrategyFactoryRepositoryAdapter",
    "MCPValidationGatewayImpl",
    "MCPVectorSearchGatewayImpl",
    "build_mcp_runtime_adapters",
]
