from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .adapters import quant as quant_adapter
from .approvals import ApprovalStore
from .broker_readonly import normalize_provider
from .gateway_route_factories import GatewayRouteFactories
from .learning_loop import LearningLoop
from .mcp_client import MCPAggregator
from .mcp_payloads import (
    classify_mcp_error as _classify_mcp_error,
    mcp_action_error_payload as _mcp_action_error_payload,
)
from .plugin_payloads import (
    plugin_self_test_payload as _plugin_self_test_payload,
    plugin_tools as _plugin_tools,
)
from .plugin_runtime import NativePluginManager
from .process_registry import ProcessRegistry
from .quant_research import QuantResearchStore
from .rl_atropos import RLAtroposManager
from .routes.ai import create_ai_router
from .routes.approvals import create_approvals_router
from .routes.connectors import create_connectors_router
from .routes.data_settings import create_data_settings_router
from .routes.finance import create_finance_router
from .routes.full_controls import create_full_controls_router
from .routes.removed import create_removed_routes_router
from .routes.research import create_research_router
from .routes.users import create_users_router
from .routes.workbench import create_workbench_router
from .routes.gateway import create_gateway_router
from .routes.health import create_health_router
from .routes.hermes import create_hermes_router
from .routes.hermes_status import create_hermes_status_router
from .routes.intents import create_intents_router
from .routes.jobs import create_jobs_router
from .routes.learning_rl import create_learning_rl_router
from .routes.mcp import create_mcp_router
from .routes.plugins_skills import create_plugins_skills_router
from .routes.responses import create_responses_router
from .routes.run_control import create_run_control_router
from .routes.run_history import create_run_history_router
from .routes.tools import create_tools_router
from .routes.webhooks import create_webhooks_router
from .routes.ws import create_ws_router
from .runtime import AgentRuntime
from .server_http_utils import cors_origins as _cors_origins
from .server_http_utils import truthy as _truthy
from .terminal_backends import list_backends, sessions as terminal_backend_sessions
from .api_envelope import ensure_envelope


@dataclass(slots=True)
class AgentRouteAssembly:
    runtime: AgentRuntime
    intent_executor: Any
    quant_store: QuantResearchStore
    build_full_runtime: Callable[[], AgentRuntime]
    reset_full_runtime: Callable[[], None]
    full_runtime_active: Callable[[], bool]
    hermes_full_enabled: Callable[[], bool]
    daemon_getter: Callable[[], Any]
    require_api: Callable[..., Any]
    require_control: Callable[..., Any]
    require_full: Callable[..., Any]
    require_user_scope: Callable[..., Any]
    control_authorized: Callable[..., tuple[bool, str | None]]
    select_runtime: Callable[..., AgentRuntime]
    tool_catalog_payload: Callable[..., dict[str, Any]]
    capabilities_payload: Callable[..., Any]
    redact_required_env: Callable[..., Any]
    parity_live_evidence: Callable[..., Any]
    settings_status_payload: Callable[..., dict[str, Any]]
    data_status_payload: Callable[..., Any]
    data_sync_plan_payload: Callable[..., Any]
    local_profile_payload: Callable[..., dict[str, Any]]
    save_local_profile: Callable[..., dict[str, Any]]
    event_batch_from_payload: Callable[..., list[dict[str, Any]]]
    request_context_payload: Callable[..., dict[str, Any]]
    factor_factory_status: Callable[..., Any]
    audited_domain_tool_call: Callable[..., Any]
    financial_catalog_payload: Callable[..., dict[str, Any]]
    financial_status_payload: Callable[..., Any]
    financial_query_payload: Callable[..., Any]
    financial_intent_payload: Callable[..., Any]
    broker_readiness_payload: Callable[..., dict[str, Any]]
    broker_sync_payload: Callable[..., Any]
    broker_snapshot_payload: Callable[..., dict[str, Any]]
    broker_resource_payload: Callable[..., dict[str, Any]]
    broker_analytics_payload: Callable[..., dict[str, Any]]
    workbench_summary_payload: Callable[..., dict[str, Any]]
    ai_status_payload: Callable[..., dict[str, Any]]
    ai_config_payload: Callable[..., dict[str, Any]]
    ai_config_save_payload: Callable[..., Any]
    ai_smoke_payload: Callable[..., Any]
    ai_models_payload: Callable[..., Any]
    messages_from_responses_payload: Callable[..., Any]
    responses_payload: Callable[..., dict[str, Any]]
    response_sse: Callable[..., Any]
    chat_completion_payload: Callable[..., dict[str, Any]]
    chat_completion_sse: Callable[..., Any]
    search_payload: Callable[..., dict[str, Any]]
    sse_events: Callable[..., Any]
    normalize_run_event: Callable[..., dict[str, Any]]
    run_trace_eval_payload: Callable[..., dict[str, Any]]
    artifact_content_payload: Callable[..., Any]
    runs_payload: Callable[..., dict[str, Any]]
    audited_tool_call: Callable[..., Any]
    metadata_allows_read_only_call: Callable[..., bool]
    is_read_only_tool: Callable[..., bool]
    read_only_result_guard: Callable[..., dict[str, Any]]
    hermes_status_payload: Callable[..., dict[str, Any]]
    hermes_readiness_payload: Callable[..., dict[str, Any]]
    financial_readiness_payload: Callable[..., Any]
    hermes_toolsets_payload: Callable[..., dict[str, Any]]
    hermes_config_payload: Callable[..., dict[str, Any]]
    hermes_sessions_payload: Callable[..., dict[str, Any]]
    hermes_session_create_payload: Callable[..., dict[str, Any]]
    hermes_handoffs_payload: Callable[..., dict[str, Any]]
    hermes_resume_context_payload: Callable[..., dict[str, Any]]
    full_tool_call: Callable[..., Any]


def configure_agent_app(app: FastAPI, routes: AgentRouteAssembly) -> None:
    # Legacy payload builders are intentionally kept domain-specific.  Adapt
    # their JSON responses at the transport boundary so every neutral API has
    # the same success/error/meta/source/freshness/degraded fields without
    # duplicating wrappers in each business handler.
    neutral_prefixes = (
        "/v1/data",
        "/v1/settings",
        "/v1/users",
        "/v1/finance",
        "/v1/research",
        "/v1/workbench",
    )

    @app.middleware("http")
    async def _neutral_api_envelope(request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith(neutral_prefixes):
            return response
        content_type = str(response.headers.get("content-type") or "").lower()
        if "application/json" not in content_type or not hasattr(response, "body_iterator"):
            return response
        raw = b"".join([chunk async for chunk in response.body_iterator])
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return response
        body = ensure_envelope(
            payload,
            status_code=response.status_code,
            path=request.url.path,
            source=f"aiask_agent.routes.{request.url.path.split('/')[2]}",
        )
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-length", "content-type"}
        }
        return JSONResponse(
            status_code=response.status_code,
            content=body,
            headers=headers,
            background=response.background,
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(_cors_origins()),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-AIASK-Agent-Token",
            "X-AIASK-Agent-Control-Token",
            "X-AIASK-Local-Control-Token",
            "X-AIASK-Session-Id",
            "X-AIASK-User-Id",
            "X-AIASK-Run-Id",
            "X-AIASK-Trace-Id",
        ],
    )

    app.include_router(
        create_health_router(
            runtime=routes.runtime,
            build_full_runtime=routes.build_full_runtime,
            hermes_full_enabled=routes.hermes_full_enabled,
            full_runtime_active=routes.full_runtime_active,
            require_api=routes.require_api,
            tool_catalog_payload=routes.tool_catalog_payload,
            capabilities_payload=routes.capabilities_payload,
            redact_required_env=routes.redact_required_env,
            parity_live_evidence=routes.parity_live_evidence,
        )
    )

    app.include_router(
        create_data_settings_router(
            require_api=routes.require_api,
            control_authorized=routes.control_authorized,
            settings_status_payload=routes.settings_status_payload,
            data_status_payload=routes.data_status_payload,
            data_sync_plan_payload=routes.data_sync_plan_payload,
        )
    )

    app.include_router(
        create_users_router(
            runtime=routes.runtime,
            require_api=routes.require_api,
            require_control=routes.require_control,
            require_user_scope=routes.require_user_scope,
            local_profile_payload=routes.local_profile_payload,
            save_local_profile=routes.save_local_profile,
            event_batch_from_payload=routes.event_batch_from_payload,
            request_context_payload=routes.request_context_payload,
            truthy=_truthy,
        )
    )

    app.include_router(
        create_finance_router(
            require_api=routes.require_api,
            control_authorized=routes.control_authorized,
            require_user_scope=routes.require_user_scope,
            audited_tool_call=routes.audited_domain_tool_call,
            quant_presets=quant_adapter.quant_presets,
            quant_store=routes.quant_store,
            financial_catalog_payload=routes.financial_catalog_payload,
            financial_status_payload=routes.financial_status_payload,
            financial_query_payload=routes.financial_query_payload,
            financial_intent_payload=routes.financial_intent_payload,
            read_only_result_guard=routes.read_only_result_guard,
            broker_readiness_payload=routes.broker_readiness_payload,
            broker_sync_payload=routes.broker_sync_payload,
            broker_snapshot_payload=routes.broker_snapshot_payload,
            broker_resource_payload=routes.broker_resource_payload,
            broker_analytics_payload=routes.broker_analytics_payload,
            session_store=routes.runtime.session_store,
            normalize_provider=normalize_provider,
        )
    )

    app.include_router(
        create_research_router(
            require_api=routes.require_api,
            factor_factory_status=routes.factor_factory_status,
            audited_tool_call=routes.audited_domain_tool_call,
        )
    )

    app.include_router(
        create_workbench_router(
            require_api=routes.require_api,
            require_control=routes.require_control,
            session_store=routes.runtime.session_store,
            workbench_summary_payload=routes.workbench_summary_payload,
        )
    )

    app.include_router(
        create_ai_router(
            require_api=routes.require_api,
            require_control=routes.require_control,
            ai_status_payload=routes.ai_status_payload,
            ai_config_payload=routes.ai_config_payload,
            ai_config_save_payload=routes.ai_config_save_payload,
            ai_smoke_payload=routes.ai_smoke_payload,
            ai_models_payload=routes.ai_models_payload,
        )
    )

    app.include_router(
        create_responses_router(
            require_api=routes.require_api,
            require_control=routes.require_control,
            select_runtime=routes.select_runtime,
            messages_from_responses_payload=routes.messages_from_responses_payload,
            responses_payload=routes.responses_payload,
            response_sse=routes.response_sse,
            chat_completion_payload=routes.chat_completion_payload,
            chat_completion_sse=routes.chat_completion_sse,
            get_response=routes.runtime.session_store.get_response,
            delete_response=routes.runtime.session_store.delete_response,
            search_payload=routes.search_payload,
        )
    )

    app.include_router(
        create_run_history_router(
            require_api=routes.require_api,
            session_store=routes.runtime.session_store,
            sse_events=routes.sse_events,
            normalize_run_event=routes.normalize_run_event,
            run_trace_eval_payload=routes.run_trace_eval_payload,
            artifact_content_payload=routes.artifact_content_payload,
            runs_payload=routes.runs_payload,
        )
    )

    app.include_router(
        create_ws_router(
            session_store=routes.runtime.session_store,
            normalize_run_event=routes.normalize_run_event,
        )
    )

    app.include_router(
        create_run_control_router(
            require_api=routes.require_api,
            require_control=routes.require_control,
            session_store=routes.runtime.session_store,
            truthy=_truthy,
        )
    )

    app.include_router(
        create_intents_router(
            require_api=routes.require_api,
            control_authorized=routes.control_authorized,
            intent_store=routes.intent_executor.store,
            intent_executor=routes.intent_executor,
            audited_domain_tool_call=routes.audited_domain_tool_call,
        )
    )

    app.include_router(
        create_approvals_router(
            require_api=routes.require_api,
            require_control=routes.require_control,
            approval_store_factory=lambda: ApprovalStore(routes.runtime.session_store.path),
        )
    )

    app.include_router(
        create_jobs_router(
            require_api=routes.require_api,
            require_control=routes.require_control,
            job_store=routes.runtime.job_store,
            scheduler=routes.runtime.scheduler,
        )
    )

    app.include_router(
        create_tools_router(
            runtime=routes.runtime,
            require_api=routes.require_api,
            require_full=routes.require_full,
            audited_tool_call=routes.audited_tool_call,
            metadata_allows_read_only_call=routes.metadata_allows_read_only_call,
            is_read_only_tool=routes.is_read_only_tool,
            read_only_result_guard=routes.read_only_result_guard,
        )
    )

    app.include_router(
        create_hermes_status_router(
            require_api=routes.require_api,
            hermes_status_payload=routes.hermes_status_payload,
            hermes_readiness_payload=routes.hermes_readiness_payload,
            financial_readiness_payload=routes.financial_readiness_payload,
        )
    )

    app.include_router(
        create_hermes_router(
            require_api=routes.require_api,
            require_control=routes.require_control,
            require_full=routes.require_full,
            hermes_toolsets_payload=routes.hermes_toolsets_payload,
            tool_catalog_payload=routes.tool_catalog_payload,
            hermes_config_payload=routes.hermes_config_payload,
            hermes_sessions_payload=routes.hermes_sessions_payload,
            hermes_session_create_payload=routes.hermes_session_create_payload,
            hermes_handoffs_payload=routes.hermes_handoffs_payload,
            hermes_resume_context_payload=routes.hermes_resume_context_payload,
        )
    )

    app.include_router(
        create_full_controls_router(
            require_full=routes.require_full,
            process_list=lambda **kwargs: ProcessRegistry(routes.runtime.session_store.path).list(**kwargs),
            list_terminal_backends=list_backends,
            terminal_sessions=lambda **kwargs: terminal_backend_sessions(
                state_path=routes.runtime.session_store.path,
                **kwargs,
            ),
        )
    )

    app.include_router(
        create_plugins_skills_router(
            require_full=routes.require_full,
            full_tool_call=routes.full_tool_call,
            intent_store=routes.intent_executor.store,
            session_store=routes.runtime.session_store,
            build_full_runtime=routes.build_full_runtime,
            plugin_manager_factory=NativePluginManager,
            plugin_self_test_payload=_plugin_self_test_payload,
            plugin_tools=_plugin_tools,
        )
    )

    def refresh_mcp_runtime() -> None:
        routes.runtime.refresh_tool_registry()
        routes.reset_full_runtime()

    app.include_router(
        create_mcp_router(
            require_api=routes.require_api,
            require_full=routes.require_full,
            full_tool_call=routes.full_tool_call,
            intent_store=routes.intent_executor.store,
            session_store=routes.runtime.session_store,
            mcp_aggregator_factory=MCPAggregator,
            refresh_mcp_runtime=refresh_mcp_runtime,
            classify_mcp_error=_classify_mcp_error,
            mcp_action_error_payload=_mcp_action_error_payload,
        )
    )

    app.include_router(
        create_learning_rl_router(
            require_full=routes.require_full,
            intent_store=routes.intent_executor.store,
            session_store=routes.runtime.session_store,
            learning_loop_factory=lambda: LearningLoop(
                session_store=routes.runtime.session_store,
                state_path=routes.runtime.session_store.path,
            ),
            rl_manager_factory=lambda: RLAtroposManager(routes.runtime.session_store.path),
        )
    )

    gateway_factories = GatewayRouteFactories(
        runtime=routes.runtime,
        app=app,
        daemon_getter=routes.daemon_getter,
    )

    app.include_router(
        create_gateway_router(
            require_api=routes.require_api,
            require_full=routes.require_full,
            full_tool_call=routes.full_tool_call,
            intent_store=routes.intent_executor.store,
            session_store=routes.runtime.session_store,
            gateway_runtime_factory=gateway_factories.runtime,
            message_store_factory=gateway_factories.message_store,
            directory_store_factory=gateway_factories.directory_store,
            config_store_factory=gateway_factories.config_store,
            delivery_router_factory=gateway_factories.delivery_router,
            adapter_for=gateway_factories.adapter_for,
            normalize_platform=gateway_factories.normalize_platform,
            gateway_daemon_status_payload=gateway_factories.daemon_status_payload,
        )
    )

    app.include_router(
        create_connectors_router(
            require_full=routes.require_full,
            connector_manager_factory=gateway_factories.connector_manager,
        )
    )

    app.include_router(
        create_webhooks_router(
            require_full=routes.require_full,
            full_tool_call=routes.full_tool_call,
            intent_store=routes.intent_executor.store,
            session_store=routes.runtime.session_store,
            webhook_store_factory=gateway_factories.webhook_store,
        )
    )

    # Registered last so it only ever matches paths no real route claimed.
    app.include_router(create_removed_routes_router())
