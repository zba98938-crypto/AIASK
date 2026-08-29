from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import HTTPException, Request

from .ai_payloads import (
    ai_config_payload_for_runtime as _ai_config_payload_for_runtime,
    ai_models_payload_for_runtime as _ai_models_payload_for_runtime,
    ai_smoke_payload_for_runtime as _ai_smoke_payload_for_runtime,
    ai_status_payload_for_runtime as _ai_status_payload_for_runtime,
    save_ai_config_for_runtime as _save_ai_config_for_runtime,
)
from .app_route_assembly import AgentRouteAssembly
from .audited_tool_calls import audited_runtime_tool_call
from .control_context import internal_control_context
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
from .financial_payloads import (
    broker_accounts_payload_for_runtime as _broker_accounts_payload_for_runtime,
    broker_resource_payload_for_runtime as _broker_resource_payload_for_runtime,
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
from .intents import IntentExecutor
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
from .route_auth import (
    RouteAuthorizer,
    control_token_configured as _control_token_configured,
    hermes_full_enabled as _hermes_full_enabled,
)
from .session_store_utils import redact_audit_text, sanitize_public_response

_UPLOAD_TEXT_PREVIEW_LIMIT = 4_000
_UPLOAD_TEXT_MIME_TYPES = {"application/json", "application/csv", "application/xml", "application/x-ndjson"}
_UPLOAD_TEXT_EXTENSIONS = (".txt", ".md", ".markdown", ".json", ".csv", ".xml", ".log", ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html")


def _public_desktop_proxy_payload(value: Any) -> Any:
    return sanitize_public_response(value, max_depth=10, max_items=500, max_text=4000)


def _public_full_tool_result(value: Any) -> dict[str, Any]:
    payload = sanitize_public_response(value, max_depth=12, max_items=500, max_text=4000)
    if isinstance(payload, dict):
        payload["secrets_redacted"] = True
        return payload
    return {"success": False, "data": None, "error": "tool returned invalid payload", "secrets_redacted": True}


def _upload_text_preview(filename: str, content_type: str, content: bytes) -> dict[str, Any]:
    is_text_like = content_type.startswith("text/") or content_type in _UPLOAD_TEXT_MIME_TYPES or filename.lower().endswith(_UPLOAD_TEXT_EXTENSIONS)
    if not is_text_like:
        return {"parse_status": "uploaded_unparsed"}
    try:
        return {
            "parse_status": "parsed_text_preview",
            "text_preview": content[:_UPLOAD_TEXT_PREVIEW_LIMIT].decode("utf-8", errors="replace"),
        }
    except Exception:
        return {"parse_status": "text_decode_failed"}


def _merge_upload_previews(payload: Any, previews: list[dict[str, Any]]) -> Any:
    if not previews:
        return payload
    if isinstance(payload, list):
        return [{**dict(item), **previews[index]} if isinstance(item, dict) and index < len(previews) else item for index, item in enumerate(payload)]
    if isinstance(payload, dict):
        next_payload = dict(payload)
        for key in ("data", "files", "items", "uploads"):
            value = next_payload.get(key)
            if isinstance(value, list):
                next_payload[key] = _merge_upload_previews(value, previews)
                return next_payload
        return {**next_payload, **previews[0]}
    return payload
from .routes.tools_catalog import build_tool_catalog_payload
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
from .runtime import AgentRuntime
from .runtime_factories import FullRuntimeManager
from .streaming_payloads import (
    chat_completion_sse_stream as _chat_completion_sse_stream,
    response_sse_stream as _response_sse_stream,
    sse_events_stream as _sse_events_stream,
)
from .gateway.http_client import _json_request, _multipart_request
from .tool_registry import SAFE_TOOL_CATALOG
from .tool_risk import metadata_is_read_only, read_only_desktop_result_guard
from .tools.policy import GENERAL_FULL_TOOLSET


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _metadata_allows_read_only_desktop_call(metadata: dict[str, Any], tool_name: str) -> bool:
    return metadata_is_read_only(metadata, target=tool_name)


def _is_read_only_desktop_tool(name: str) -> bool:
    if name == "agent_tool_catalog":
        return True
    for item in SAFE_TOOL_CATALOG:
        if item.get("name") == name:
            return item.get("side_effect") == "read_only"
    return False


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


@dataclass(slots=True)
class AppRouteCallbackFactory:
    runtime: AgentRuntime
    intent_executor: IntentExecutor
    full_runtime_manager: FullRuntimeManager
    quant_store: QuantResearchStore
    factor_factory_status: Callable[..., Any]
    authorizer: RouteAuthorizer = field(init=False)

    def __post_init__(self) -> None:
        self.authorizer = RouteAuthorizer(
            runtime=self.runtime,
            build_full_runtime=self.build_full_runtime,
            request_user_id_from_payload=_request_user_id_from_payload,
        )
        self.intent_executor.set_confirmed_action_executor(self.execute_confirmed_action)

    def build_full_runtime(self) -> AgentRuntime:
        return self.full_runtime_manager.build()

    async def execute_confirmed_action(
        self,
        target_tool: str,
        target_action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        from .adapters.confirmed_actions import execute_confirmed_action

        runtime = (
            self.build_full_runtime()
            if str(target_tool or "").strip() in {"browser", "terminal", "process", "code"}
            else None
        )
        result = await execute_confirmed_action(target_tool, target_action, params, runtime=runtime)
        if str(target_tool or "").strip() in {"acp", "mcp", "plugin", "skill", "skill_pack"} and result.get("success") is not False:
            self.runtime.refresh_tool_registry()
            self.full_runtime_manager.reset()
        return result

    def event_batch_from_payload(self, payload: dict[str, Any], request: Request) -> list[dict[str, Any]]:
        context = _request_context_payload(payload, headers=request.headers)
        raw_events = payload.get("events")
        events = raw_events if isinstance(raw_events, list) else [payload]
        normalized: list[dict[str, Any]] = []
        for item in list(events or [])[:200]:
            if not isinstance(item, dict):
                continue
            event = {**context, **dict(item)}
            event.setdefault("user_id", context["user_id"])
            event.setdefault("session_id", context["session_id"])
            event.setdefault("run_id", context["run_id"])
            event.setdefault("trace_id", context["trace_id"])
            event.setdefault("source", context["source"])
            normalized.append(event)
        return normalized

    async def audited_tool_call(
        self,
        selected: AgentRuntime,
        tool_name: str,
        payload: dict[str, Any],
        *,
        request: Request,
        metadata: dict[str, Any] | None = None,
        source_chain: list[str] | None = None,
        result_guard: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await _audited_runtime_tool_call(
            selected,
            tool_name,
            dict(payload or {}),
            headers=request.headers,
            metadata=metadata,
            source_chain=source_chain,
            result_guard=result_guard,
        )

    async def audited_domain_tool_call(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        request: Request,
        source_chain: list[str],
        result_guard: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tool = self.runtime.tool_registry.get(tool_name)
        metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
        return await _audited_runtime_tool_call(
            self.runtime,
            tool_name,
            dict(payload or {}),
            headers=request.headers,
            metadata=metadata,
            source_chain=source_chain,
            result_guard=result_guard,
        )

    def tool_catalog_payload(self, selected: AgentRuntime, *, implementation: str | None = None) -> dict[str, Any]:
        return build_tool_catalog_payload(selected, implementation=implementation)

    async def full_tool_call(self, request: Request, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        full = self.authorizer.require_full(request)
        tool = full.tool_registry.get(tool_name)
        with internal_control_context("fastapi.full_tool_call"):
            result = await _audited_runtime_tool_call(
                full,
                tool_name,
                dict(arguments or {}),
                headers=request.headers,
                metadata=dict(getattr(tool, "metadata", {}) or {}) if tool else {},
                source_chain=["aiask_agent.server", "full_tool_call"],
            )
        public_result = _public_full_tool_result(result)
        if not public_result.get("success"):
            raise HTTPException(400, detail=public_result.get("error") or "tool failed")
        return public_result

    def hermes_readiness_payload(self) -> dict[str, Any]:
        return _hermes_readiness_payload_for_runtime(
            self.runtime,
            build_full_runtime=self.build_full_runtime,
            hermes_full_enabled=_hermes_full_enabled,
        )

    def ai_status_payload(self) -> dict[str, Any]:
        return _ai_status_payload_for_runtime(self.runtime)

    async def ai_smoke_payload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _ai_smoke_payload_for_runtime(self.runtime, payload)

    async def ai_models_payload(self) -> dict[str, Any]:
        return await _ai_models_payload_for_runtime(self.runtime)

    def ai_config_payload(self) -> dict[str, Any]:
        return _ai_config_payload_for_runtime(self.runtime)

    async def ai_config_save_payload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _save_ai_config_for_runtime(self.runtime, payload)

    def workbench_summary_payload(
        self,
        *,
        user_id: str | None = None,
        session_limit: int = 8,
        run_limit: int = 8,
    ) -> dict[str, Any]:
        return _workbench_summary_payload(
            self.runtime,
            intent_store=self.intent_executor.store,
            user_id=user_id,
            session_limit=session_limit,
            run_limit=run_limit,
        )

    def runs_payload(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return _desktop_runs_payload(self.runtime, session_id=session_id, status=status, limit=limit)

    def search_payload(
        self,
        *,
        query: str = "",
        q: str = "",
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return {
            "object": "list",
            "include_archived": bool(include_archived),
            "data": self.runtime.session_store.search(
                query=query or q,
                session_id=session_id,
                user_id=user_id,
                limit=limit,
                include_archived=bool(include_archived),
            ),
        }

    def run_trace_eval_payload(self, run_id: str) -> dict[str, Any]:
        return _run_trace_eval_payload(self.runtime, run_id)

    def artifact_content_payload(self, artifact_id: str, *, max_bytes: int = 262144) -> dict[str, Any]:
        return _artifact_content_payload(self.runtime.session_store, artifact_id, max_bytes=max_bytes)

    def hermes_status_payload(self) -> dict[str, Any]:
        return _hermes_status_payload_for_runtime(
            self.runtime,
            build_full_runtime=self.build_full_runtime,
            hermes_full_enabled=_hermes_full_enabled,
            full_runtime_active=self.full_runtime_manager.active,
        )

    async def financial_readiness_payload(self) -> dict[str, Any]:
        return await _financial_readiness_payload_for_runtime(
            self.runtime,
            build_full_runtime=self.build_full_runtime,
            hermes_full_enabled=_hermes_full_enabled,
            current_full_runtime=self.full_runtime_manager.current,
            control_token_configured=_control_token_configured(),
            ai_status=self.ai_status_payload(),
            intent_store=self.intent_executor.store,
        )

    def hermes_toolsets_payload(self) -> dict[str, Any]:
        return {
            "object": "list",
            "active": "finance_safe",
            "data": [
                {"name": "finance_safe", "implementation": "aiask_native", "default": True},
                {
                    "name": "hermes_full",
                    "implementation": "aiask_native",
                    "enabled": _hermes_full_enabled(),
                    "toolset": GENERAL_FULL_TOOLSET,
                },
            ],
        }

    def hermes_config_payload(self, full: AgentRuntime) -> dict[str, Any]:
        return {
            "object": "aiask.hermes_config",
            "home": os.getenv("AIASK_AGENT_HOME", ""),
            "toolset": full.tool_registry.policy_engine.toolset,
            "workspace_roots": list(full.tool_registry.policy_engine.policy.workspace_roots),
            "secrets_redacted": True,
        }

    def hermes_sessions_payload(
        self,
        *,
        user_id: str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return {
            "object": "list",
            "implementation": "aiask_native",
            "include_archived": bool(include_archived),
            "data": _session_summary_payload(
                self.runtime,
                intent_store=self.intent_executor.store,
                user_id=user_id,
                limit=limit,
                include_archived=include_archived,
            ),
        }

    def hermes_session_create_payload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        user_id = str(body.get("user_id") or "local").strip() or "local"
        title = str(body.get("title") or "").strip() or None
        description = str(body.get("description") or "").strip() or None
        initial_context = str(body.get("initial_context") or "").strip() or None
        metadata: dict[str, Any] = {}
        if description:
            metadata["description"] = description
        if initial_context:
            metadata["initial_context"] = initial_context

        session_id = self.runtime.session_store.create_session(user_id=user_id, title=title, metadata=metadata or None)

        if initial_context:
            self.runtime.session_store.append_message(
                session_id,
                {
                    "role": "system",
                    "name": "desktop_initial_context",
                    "content": initial_context,
                    "metadata": {"source": "desktop_create_session"},
                },
            )

        session = self.runtime.session_store.get_session(session_id)
        assert session is not None
        summary = _session_summary_payload(
            self.runtime,
            intent_store=self.intent_executor.store,
            user_id=user_id,
            limit=200,
            include_archived=True,
        )
        created = next((item for item in summary if str(item.get("session_id") or "") == session_id), None)
        return {
            "object": "aiask.session",
            "implementation": "aiask_native",
            "success": True,
            "data": created or {
                "session_id": session_id,
                "title": session.get("title") or session_id,
                "user_id": user_id,
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
                "message_count": self.runtime.session_store.count_session_messages(session_id),
                "status": "idle",
                "archived": False,
                "metadata": dict(session.get("metadata") or {}),
            },
        }

    def hermes_handoffs_payload(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        include_completed: bool = False,
    ) -> dict[str, Any]:
        return _handoff_queue_payload(
            self.runtime,
            user_id=user_id,
            session_id=session_id,
            status=status,
            limit=limit,
            include_completed=include_completed,
        )

    def hermes_resume_context_payload(self, session_id: str) -> dict[str, Any]:
        return _session_resume_context_payload(
            self.runtime,
            session_id,
            intent_store=self.intent_executor.store,
        )

    def settings_status_payload(self, request: Request | None = None) -> dict[str, Any]:
        control_ok = False
        control_reason = None
        if request is not None:
            control_ok, control_reason = self.authorizer.control_authorized(request)
        return _desktop_settings_status_payload_builder(
            self.runtime,
            ai_status_payload=_ai_status_payload_for_runtime,
            endpoint=_agent_endpoint(),
            control_authorized=control_ok,
            control_reason=control_reason,
        )

    async def data_status_payload(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _desktop_data_status_payload_for_runtime(self.runtime, arguments)

    async def data_sync_plan_payload(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _desktop_data_sync_plan_payload_for_runtime(self.runtime, arguments)

    async def capabilities_payload(self, request: Request) -> dict[str, Any]:
        return await _desktop_capabilities_payload_for_runtime(
            self.runtime,
            request,
            build_full_runtime=self.build_full_runtime,
            current_full_runtime=self.full_runtime_manager.current,
            full_authorized=self.authorizer.full_authorized,
            control_authorized=self.authorizer.control_authorized,
            hermes_full_enabled=_hermes_full_enabled,
            hermes_readiness_payload=self.hermes_readiness_payload,
            quant_store=self.quant_store,
            ai_status_payload=self.ai_status_payload,
            intent_store=self.intent_executor.store,
        )

    async def sse_events(self, events: list[dict[str, Any]]) -> AsyncIterator[bytes]:
        async for chunk in _sse_events_stream(events, normalize_run_event=_normalize_run_event):
            yield chunk

    async def chat_completion_sse(self, result: Any, *, model: str) -> AsyncIterator[bytes]:
        async for chunk in _chat_completion_sse_stream(result, model=model):
            yield chunk

    async def response_sse(self, result: Any, *, model: str) -> AsyncIterator[bytes]:
        async for chunk in _response_sse_stream(result, model=model):
            yield chunk

    def financial_catalog_payload(self) -> dict[str, Any]:
        return _financial_catalog_payload_for_runtime(self.runtime)

    async def financial_status_payload(self) -> dict[str, Any]:
        return await _financial_status_payload_for_runtime(
            self.runtime,
            intent_store=self.intent_executor.store,
        )

    async def financial_query_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await _financial_query_payload_for_runtime(self.runtime, payload, **kwargs)

    async def financial_intent_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await _financial_intent_payload_for_runtime(self.runtime, payload, **kwargs)

    def broker_readiness_payload(self) -> dict[str, Any]:
        return _broker_readiness_payload_for_runtime(self.runtime)

    async def broker_sync_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await _broker_sync_payload_for_runtime(self.runtime, payload, **kwargs)

    def broker_snapshot_payload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return _broker_accounts_payload_for_runtime(self.runtime, payload)

    def broker_resource_payload(self, resource: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return _broker_resource_payload_for_runtime(self.runtime, resource, payload)

    def broker_analytics_payload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return _broker_analytics_payload_for_runtime(self.runtime, payload)

    def route_assembly(self, *, daemon_getter: Callable[[], Any]) -> AgentRouteAssembly:
        return AgentRouteAssembly(
            runtime=self.runtime,
            intent_executor=self.intent_executor,
            quant_store=self.quant_store,
            build_full_runtime=self.build_full_runtime,
            reset_full_runtime=self.full_runtime_manager.reset,
            full_runtime_active=self.full_runtime_manager.active,
            hermes_full_enabled=_hermes_full_enabled,
            daemon_getter=daemon_getter,
            require_api=self.authorizer.require_api,
            require_control=self.authorizer.require_control,
            require_full=self.authorizer.require_full,
            require_user_scope=self.authorizer.require_user_scope,
            control_authorized=self.authorizer.control_authorized,
            select_runtime=self.authorizer.select_runtime,
            tool_catalog_payload=self.tool_catalog_payload,
            capabilities_payload=self.capabilities_payload,
            redact_required_env=_redact_required_env,
            parity_live_evidence=_parity_live_evidence,
            settings_status_payload=self.settings_status_payload,
            data_status_payload=self.data_status_payload,
            data_sync_plan_payload=self.data_sync_plan_payload,
            local_profile_payload=local_profile_payload,
            save_local_profile=save_local_profile,
            event_batch_from_payload=self.event_batch_from_payload,
            request_context_payload=_request_context_payload,
            factor_factory_status=self.factor_factory_status,
            audited_domain_tool_call=self.audited_domain_tool_call,
            financial_catalog_payload=self.financial_catalog_payload,
            financial_status_payload=self.financial_status_payload,
            financial_query_payload=self.financial_query_payload,
            financial_intent_payload=self.financial_intent_payload,
            broker_readiness_payload=self.broker_readiness_payload,
            broker_sync_payload=self.broker_sync_payload,
            broker_snapshot_payload=self.broker_snapshot_payload,
            broker_resource_payload=self.broker_resource_payload,
            broker_analytics_payload=self.broker_analytics_payload,
            workbench_summary_payload=self.workbench_summary_payload,
            ai_status_payload=self.ai_status_payload,
            ai_config_payload=self.ai_config_payload,
            ai_config_save_payload=self.ai_config_save_payload,
            ai_smoke_payload=self.ai_smoke_payload,
            ai_models_payload=self.ai_models_payload,
            messages_from_responses_payload=_messages_from_responses_payload,
            responses_payload=_responses_payload,
            response_sse=self.response_sse,
            chat_completion_payload=_chat_completion_payload,
            chat_completion_sse=self.chat_completion_sse,
            search_payload=self.search_payload,
            sse_events=self.sse_events,
            normalize_run_event=_normalize_run_event,
            run_trace_eval_payload=self.run_trace_eval_payload,
            artifact_content_payload=self.artifact_content_payload,
            runs_payload=self.runs_payload,
            audited_tool_call=self.audited_tool_call,
            metadata_allows_read_only_call=_metadata_allows_read_only_desktop_call,
            is_read_only_tool=_is_read_only_desktop_tool,
            read_only_result_guard=read_only_desktop_result_guard,
            hermes_status_payload=self.hermes_status_payload,
            hermes_readiness_payload=self.hermes_readiness_payload,
            financial_readiness_payload=self.financial_readiness_payload,
            hermes_toolsets_payload=self.hermes_toolsets_payload,
            hermes_config_payload=self.hermes_config_payload,
            hermes_sessions_payload=self.hermes_sessions_payload,
            hermes_session_create_payload=self.hermes_session_create_payload,
            hermes_handoffs_payload=self.hermes_handoffs_payload,
            hermes_resume_context_payload=self.hermes_resume_context_payload,
            full_tool_call=self.full_tool_call,
        )
