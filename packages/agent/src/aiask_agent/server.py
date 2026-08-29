from __future__ import annotations

import argparse
import asyncio
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .acp import ACPManager
from .app_route_callbacks import AppRouteCallbackFactory
from .app_route_assembly import AgentRouteAssembly, configure_agent_app
from .app_lifecycle import AgentAppLifecycle
from .ai_payloads import (
    ai_config_payload_for_runtime as _ai_config_payload_for_runtime,
    ai_models_payload_for_runtime as _ai_models_payload_for_runtime,
    ai_smoke_payload_for_runtime as _ai_smoke_payload_for_runtime,
    ai_status_payload_for_runtime as _ai_status_payload_for_runtime,
    save_ai_config_for_runtime as _save_ai_config_for_runtime,
)
from .audited_tool_calls import audited_runtime_tool_call
from .broker_readonly import normalize_provider
from .capabilities import HERMES_BASELINE, HERMES_BASELINE_VERSION, HERMES_RELEASE_TAG, parity_summary
from .settings_payloads import (
    agent_endpoint as _agent_endpoint,
    data_status_payload_for_runtime as _desktop_data_status_payload_for_runtime,
    data_sync_plan_payload_for_runtime as _desktop_data_sync_plan_payload_for_runtime,
    settings_status_payload_for_runtime as _desktop_settings_status_payload_builder,
    local_profile_payload,
    save_local_profile,
)
from .capabilities_payloads import (
    capabilities_payload_for_runtime as _desktop_capabilities_payload_for_runtime,
)
from .env_config import load_project_env
from .gateway import ADAPTERS
from .financial_payloads import (
    broker_accounts_payload_for_runtime as _broker_accounts_payload_for_runtime,
    broker_analytics_payload_for_runtime as _broker_analytics_payload_for_runtime,
    broker_readiness_payload_for_runtime as _broker_readiness_payload_for_runtime,
    broker_sync_payload_for_runtime as _broker_sync_payload_for_runtime,
    financial_catalog_payload_for_runtime as _financial_catalog_payload_for_runtime,
    financial_intent_payload_for_runtime as _financial_intent_payload_for_runtime,
    financial_query_payload_for_runtime as _financial_query_payload_for_runtime,
    financial_status_payload_for_runtime as _financial_status_payload_for_runtime,
)
from .hermes_payloads import (
    financial_readiness_payload_for_runtime as _financial_readiness_payload_for_runtime,
    hermes_readiness_payload_for_runtime as _hermes_readiness_payload_for_runtime,
    hermes_status_payload_for_runtime as _hermes_status_payload_for_runtime,
    parity_live_evidence as _parity_live_evidence,
    redact_required_env as _redact_required_env,
)
from .learning_loop import LearningLoop
from .intents import ActionIntentStore, IntentExecutor
from .mcp_client import MCPAggregator
from .mcp_payloads import (
    classify_mcp_error as _classify_mcp_error,
    mcp_action_error_payload as _mcp_action_error_payload,
)
from .memory_providers import MemoryProviderManager
from .model_providers import ModelProviderRegistry, ProviderUsageStore
from .plugin_payloads import (
    plugin_self_test_payload as _plugin_self_test_payload,
    plugin_tools as _plugin_tools,
    public_plugin_test_payload as _public_plugin_test_payload,
)
from .plugin_runtime import NativePluginManager
from .process_registry import ProcessRegistry
from .quant_research import QuantResearchStore
from .request_context import (
    request_context_payload as _request_context_payload,
    request_user_id_from_payload as _request_user_id_from_payload,
)
from .response_payloads import (
    chat_completion_payload as _chat_completion_payload,
    messages_from_responses_payload as _messages_from_responses_payload,
    responses_payload as _responses_payload,
)
from .rl_atropos import RLAtroposManager
from .run_payloads import (
    _artifact_content_payload,
    _desktop_runs_payload,
    _handoff_queue_payload,
    _normalize_run_event,
    _run_trace_eval_payload,
    _session_resume_context_payload,
    _session_summary_payload,
    _workbench_summary_payload,
)
from .runtime_factories import (
    FullRuntimeManager,
    build_runtime_and_executor as _build_runtime_and_executor,
)
from .security import SecurityScanner
from .runtime import AgentRuntime
from .route_auth import (
    RouteAuthorizer,
    control_token_configured as _control_token_configured,
    hermes_full_enabled as _hermes_full_enabled,
    is_loopback as _is_loopback,
    mode_error_status as _mode_error_status,
)
from .server_http_utils import (
    cors_origins as _cors_origins,
    header_token as _header_token,
    json_dumps as _json_dumps,
    query_bool as _query_bool,
    read_json as _read_json,
    truthy as _truthy,
)
from .routes.tools_catalog import build_tool_catalog_payload
from .session_store import AgentSessionStore
from .session_store_utils import redact_audit_text, sanitize_public_response
from .skill_packs import SkillPackManager
from .stock_data_sources import list_stock_data_sources, save_stock_data_source, test_stock_data_source
from .streaming_payloads import (
    chat_completion_sse_stream as _chat_completion_sse_stream,
    response_sse_stream as _response_sse_stream,
    sse_events_stream as _sse_events_stream,
)
from .native_capabilities import SkillStore
from .terminal_backends import list_backends, sessions as terminal_backend_sessions
from .tui import status as tui_status_payload
from .tool_registry import SAFE_TOOL_CATALOG
from .tool_risk import metadata_is_read_only, read_only_desktop_result_guard
from .tools.policy import GENERAL_FULL_TOOLSET
from .adapters import quant as quant_adapter
from .adapters.confirmed_actions import factor_factory_status


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _load_local_env_file() -> None:
    """Load a repo-local .env for CLI/dev launches without overriding the process env."""
    load_project_env()


def _public_http_exception_payload(exc: HTTPException) -> dict[str, Any]:
    detail = exc.detail
    if isinstance(detail, str):
        public_detail: Any = redact_audit_text(detail)
    else:
        public_detail = sanitize_public_response(detail, max_depth=8, max_items=200, max_text=4000)
    return {"detail": public_detail, "secrets_redacted": True}


def _public_validation_exception_payload(exc: RequestValidationError) -> dict[str, Any]:
    return {
        "detail": sanitize_public_response(exc.errors(), max_depth=8, max_items=200, max_text=4000),
        "secrets_redacted": True,
    }


async def _audited_runtime_tool_call(
    selected: AgentRuntime,
    tool_name: str,
    payload: dict[str, Any],
    *,
    headers: Any | None = None,
    metadata: dict[str, Any] | None = None,
    source_chain: list[str] | None = None,
    result_guard: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return await audited_runtime_tool_call(
        selected,
        tool_name,
        payload,
        request_context_payload=_request_context_payload,
        headers=headers,
        metadata=metadata,
        source_chain=source_chain,
        result_guard=result_guard,
    )


def _is_read_only_desktop_tool(name: str) -> bool:
    if name == "agent_tool_catalog":
        return True
    for item in SAFE_TOOL_CATALOG:
        if item.get("name") == name:
            return item.get("side_effect") == "read_only"
    return False


def _metadata_allows_read_only_desktop_call(metadata: dict[str, Any], tool_name: str) -> bool:
    return metadata_is_read_only(metadata, target=tool_name)


def _desktop_settings_status_payload_for_runtime(
    runtime: AgentRuntime,
    *,
    endpoint: str | None = None,
    control_authorized: bool = False,
    control_reason: str | None = None,
) -> dict[str, Any]:
    return _desktop_settings_status_payload_builder(
        runtime,
        ai_status_payload=_ai_status_payload_for_runtime,
        endpoint=endpoint,
        control_authorized=control_authorized,
        control_reason=control_reason,
    )


def _financial_catalog_payload(runtime: AgentRuntime) -> dict[str, Any]:
    return _financial_catalog_payload_for_runtime(runtime)


async def _financial_status_payload(
    runtime: AgentRuntime,
    *,
    intent_store: ActionIntentStore | None = None,
) -> dict[str, Any]:
    return await _financial_status_payload_for_runtime(runtime, intent_store=intent_store)


async def _financial_query_payload(runtime: AgentRuntime, payload: dict[str, Any], *, tool_caller: ToolCaller | None = None) -> dict[str, Any]:
    return await _financial_query_payload_for_runtime(runtime, payload, tool_caller=tool_caller)


def _broker_readiness_payload(runtime: AgentRuntime) -> dict[str, Any]:
    return _broker_readiness_payload_for_runtime(runtime)


def _broker_accounts_payload(runtime: AgentRuntime, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return _broker_accounts_payload_for_runtime(runtime, payload)


async def _broker_sync_payload(
    runtime: AgentRuntime,
    payload: dict[str, Any],
    *,
    headers: Any | None = None,
    tool_caller: ToolCaller | None = None,
) -> dict[str, Any]:
    return await _broker_sync_payload_for_runtime(runtime, payload, headers=headers, tool_caller=tool_caller)


def _broker_analytics_payload(runtime: AgentRuntime, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return _broker_analytics_payload_for_runtime(runtime, payload)


async def _financial_intent_payload(runtime: AgentRuntime, payload: dict[str, Any], *, tool_caller: ToolCaller | None = None) -> dict[str, Any]:
    return await _financial_intent_payload_for_runtime(runtime, payload, tool_caller=tool_caller)


def create_app(
    *,
    runtime: AgentRuntime | None = None,
    intent_executor: IntentExecutor | None = None,
) -> FastAPI:
    load_project_env()
    if runtime is None:
        runtime, default_executor = _build_runtime_and_executor()
        intent_executor = intent_executor or default_executor
    if intent_executor is None:
        intent_executor = IntentExecutor(ActionIntentStore())
    full_runtime_manager = FullRuntimeManager(runtime, intent_executor.store)
    quant_store = QuantResearchStore(runtime.session_store.path)
    callbacks = AppRouteCallbackFactory(
        runtime=runtime,
        intent_executor=intent_executor,
        full_runtime_manager=full_runtime_manager,
        quant_store=quant_store,
        factor_factory_status=factor_factory_status,
    )

    lifecycle = AgentAppLifecycle(runtime=runtime, full_runtime_getter=full_runtime_manager.current)
    app = FastAPI(title="AIASK Agent", version="0.1.0", lifespan=lifecycle.lifespan)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_public_http_exception_payload(exc),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content=_public_validation_exception_payload(exc))

    configure_agent_app(app, callbacks.route_assembly(daemon_getter=lifecycle.daemon))

    return app


def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    runtime: AgentRuntime | None = None,
    intent_executor: IntentExecutor | None = None,
) -> ThreadingHTTPServer:
    from .fallback_server import build_server as build_fallback_server

    return build_fallback_server(
        host=host,
        port=port,
        runtime=runtime,
        intent_executor=intent_executor,
    )

def main(argv: list[str] | None = None) -> None:
    from .server_cli import main as server_cli_main

    server_cli_main(argv)


if __name__ == "__main__":
    main()
