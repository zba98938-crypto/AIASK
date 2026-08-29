from __future__ import annotations

import asyncio
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .intents import public_intents
from .plugin_runtime import _safe_slug
from .route_auth import token_matches
from .session_store_utils import redact_audit_text, sanitize_public_response

# The compatibility server used to copy every global from ``server.py`` into
# this module at runtime. That made dependencies invisible to static analysis
# and let the ASGI/legacy implementations drift silently. Import the exact
# compatibility surface explicitly instead.
from .server import (
    ACPManager,
    ADAPTERS,
    ActionIntentStore,
    FullRuntimeManager,
    GENERAL_FULL_TOOLSET,
    HERMES_BASELINE,
    HERMES_BASELINE_VERSION,
    HERMES_RELEASE_TAG,
    IntentExecutor,
    MCPAggregator,
    MemoryProviderManager,
    ModelProviderRegistry,
    NativePluginManager,
    ProviderUsageStore,
    QuantResearchStore,
    SecurityScanner,
    SkillPackManager,
    SkillStore,
    _ai_config_payload_for_runtime,
    _ai_models_payload_for_runtime,
    _ai_smoke_payload_for_runtime,
    _ai_status_payload_for_runtime,
    _artifact_content_payload,
    _audited_runtime_tool_call,
    _broker_accounts_payload,
    _broker_analytics_payload,
    _broker_readiness_payload,
    _broker_sync_payload,
    _build_runtime_and_executor,
    _chat_completion_payload,
    _cors_origins,
    _desktop_data_status_payload_for_runtime,
    _desktop_data_sync_plan_payload_for_runtime,
    _desktop_runs_payload,
    _desktop_settings_status_payload_for_runtime,
    _financial_catalog_payload,
    _financial_intent_payload,
    _financial_query_payload,
    _financial_status_payload,
    _handoff_queue_payload,
    _header_token,
    _hermes_full_enabled,
    _is_loopback,
    _is_read_only_desktop_tool,
    _json_dumps,
    _mcp_action_error_payload,
    _messages_from_responses_payload,
    _metadata_allows_read_only_desktop_call,
    _normalize_run_event,
    _parity_live_evidence,
    _plugin_self_test_payload,
    _plugin_tools,
    _public_plugin_test_payload,
    _query_bool,
    _read_json,
    _redact_required_env,
    _request_context_payload,
    _request_user_id_from_payload,
    _responses_payload,
    _run_trace_eval_payload,
    _save_ai_config_for_runtime,
    _session_resume_context_payload,
    _session_summary_payload,
    _truthy,
    _workbench_summary_payload,
    build_tool_catalog_payload,
    factor_factory_status,
    list_stock_data_sources,
    load_project_env,
    local_profile_payload,
    normalize_provider,
    parity_summary,
    quant_adapter,
    read_only_desktop_result_guard,
    save_local_profile,
    save_stock_data_source,
    test_stock_data_source,
)

def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    runtime: AgentRuntime | None = None,
    intent_executor: IntentExecutor | None = None,
) -> ThreadingHTTPServer:
    load_project_env()
    if runtime is None:
        default_runtime, default_executor = _build_runtime_and_executor()
        runtime = default_runtime
        intent_executor = intent_executor or default_executor
    if intent_executor is None:
        intent_executor = IntentExecutor(ActionIntentStore())
    full_runtime_manager = FullRuntimeManager(runtime, intent_executor.store)
    quant_store = QuantResearchStore(runtime.session_store.path)

    def _build_native_full_runtime() -> AgentRuntime:
        return full_runtime_manager.build()

    async def _execute_confirmed_action(
        target_tool: str,
        target_action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        from .adapters.desktop_ops import execute_confirmed_action

        selected_runtime = (
            _build_native_full_runtime()
            if str(target_tool or "").strip() in {"browser", "terminal", "process", "code"}
            else None
        )
        result = await execute_confirmed_action(target_tool, target_action, params, runtime=selected_runtime)
        if str(target_tool or "").strip() in {"acp", "mcp", "plugin", "skill", "skill_pack"} and result.get("success") is not False:
            runtime.refresh_tool_registry()
            full_runtime_manager.reset()
        return result

    intent_executor.set_confirmed_action_executor(_execute_confirmed_action)

    class AIASKAgentHTTPServer(ThreadingHTTPServer):
        def server_close(self) -> None:
            try:
                full_runtime_manager.close()
                runtime.close()
            finally:
                super().server_close()

    class AIASKAgentHandler(BaseHTTPRequestHandler):
        server_version = "AIASKAgent/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            if os.getenv("AIASK_AGENT_HTTP_LOGS", "").strip() == "1":
                super().log_message(format, *args)

        def _send_json(self, status: int, payload: Any) -> None:
            body = _json_dumps(sanitize_public_response(payload, max_depth=10, max_items=500, max_text=4000))
            self.send_response(status)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_sse(self, events: list[dict[str, Any]]) -> None:
            chunks: list[bytes] = []
            for raw_event in events:
                event = _normalize_run_event(raw_event)
                if event.get("id") is not None:
                    chunks.append(f"id: {event['id']}\n".encode("utf-8"))
                if event.get("event"):
                    chunks.append(f"event: {event['event']}\n".encode("utf-8"))
                chunks.append(b"data: ")
                chunks.append(_json_dumps(event))
                chunks.append(b"\n\n")
            body = b"".join(chunks)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def _send_chat_completion_sse(self, result: Any, *, model: str) -> None:
            created = int(time.time())
            events = [
                {
                    "data": {
                        "id": result.response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    }
                },
                {
                    "data": {
                        "id": result.response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": result.content}, "finish_reason": None}],
                        "aiask": {"session_id": result.session_id, "run_id": result.run_id},
                    }
                },
                {
                    "data": {
                        "id": result.response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                },
                {"data": "[DONE]"},
            ]
            chunks: list[bytes] = []
            for event in events:
                chunks.append(b"data: ")
                data = event["data"]
                chunks.append(b"[DONE]" if data == "[DONE]" else _json_dumps(data))
                chunks.append(b"\n\n")
            body = b"".join(chunks)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def _send_response_sse(self, result: Any, *, model: str) -> None:
            events = [
                {
                    "event": "response.created",
                    "data": {"id": result.response_id, "status": "in_progress", "model": model},
                },
                {
                    "event": "response.output_text.delta",
                    "data": {"id": result.response_id, "delta": result.content},
                },
                {
                    "event": "response.completed",
                    "data": {"id": result.response_id, "status": result.status, "run_id": result.run_id},
                },
                {"data": "[DONE]"},
            ]
            chunks: list[bytes] = []
            for event in events:
                if event.get("event"):
                    chunks.append(f"event: {event['event']}\n".encode("utf-8"))
                chunks.append(b"data: ")
                data = event["data"]
                chunks.append(b"[DONE]" if data == "[DONE]" else _json_dumps(data))
                chunks.append(b"\n\n")
            body = b"".join(chunks)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def _send_cors_headers(self) -> None:
            origin = str(self.headers.get("Origin") or "").strip().rstrip("/")
            if origin and origin in _cors_origins():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    ", ".join(
                        [
                            "Authorization",
                            "Content-Type",
                            "X-AIASK-Agent-Token",
                            "X-AIASK-Agent-Control-Token",
                            "X-AIASK-Local-Control-Token",
                            "X-AIASK-Session-Id",
                            "X-AIASK-User-Id",
                            "X-AIASK-Run-Id",
                            "X-AIASK-Trace-Id",
                        ]
                    ),
                )

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._send_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_error_json(self, status: int, message: str, *, code: str | None = None) -> None:
            payload = {
                "error": {"message": redact_audit_text(message), "type": code or HTTPStatus(status).phrase},
                "secrets_redacted": True,
            }
            self._send_json(status, payload)

        def _api_authorized(self) -> bool:
            bind_host = str(self.server.server_address[0])
            if _is_loopback(bind_host):
                return True
            expected = str(os.getenv("AIASK_AGENT_API_TOKEN", "")).strip()
            if not expected:
                return False
            return token_matches(_header_token(self, "X-AIASK-Agent-Token"), expected)

        def _control_authorized(self) -> tuple[bool, str | None]:
            client_host = str(self.client_address[0])
            bind_host = str(self.server.server_address[0])
            if not _is_loopback(client_host) or not _is_loopback(bind_host):
                return False, "control endpoint is loopback only"
            expected = (
                str(os.getenv("AIASK_AGENT_CONTROL_TOKEN", "")).strip()
                or str(os.getenv("AIASK_LOCAL_CONTROL_TOKEN", "")).strip()
            )
            if not expected:
                return False, "control token is not configured"
            token = _header_token(self, "X-AIASK-Agent-Control-Token", "X-AIASK-Local-Control-Token")
            if not token_matches(token, expected):
                return False, "invalid control token"
            return True, None

        def _hermes_full_authorized(self) -> tuple[bool, str | None]:
            if not _hermes_full_enabled():
                return False, "AIASK native Hermes full mode is not enabled"
            return self._control_authorized()

        def _mode_runtime(self, payload: dict[str, Any]) -> tuple[AgentRuntime | None, str, tuple[bool, str | None]]:
            mode = str(payload.get("mode") or "finance_safe").strip() or "finance_safe"
            if mode == "finance_safe":
                return runtime, mode, (True, None)
            if mode == "hermes_full":
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    return None, mode, (False, reason)
                return _build_native_full_runtime(), mode, (True, None)
            return None, mode, (False, f"unsupported mode: {mode}")

        def _require_user_scope(self, requested_user_id: str | None) -> tuple[bool, str, int, str | None]:
            current_user_id = _request_user_id_from_payload({}, headers=self.headers)
            requested = str(requested_user_id or current_user_id or "local").strip() or "local"
            if requested != current_user_id:
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    return False, requested, status, reason or "cross-user access requires control token"
            return True, requested, 200, None

        def _event_batch_from_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
            context = _request_context_payload(payload, headers=self.headers)
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

        def _audited_tool_call(
            self,
            selected: AgentRuntime,
            tool_name: str,
            payload: dict[str, Any],
            *,
            metadata: dict[str, Any] | None = None,
            source_chain: list[str] | None = None,
            result_guard: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            return asyncio.run(
                _audited_runtime_tool_call(
                    selected,
                    tool_name,
                    dict(payload or {}),
                    headers=self.headers,
                    metadata=metadata,
                    source_chain=source_chain,
                    result_guard=result_guard,
                )
            )

        def _audited_desktop_tool_call(self, tool_name: str, payload: dict[str, Any], *, source_chain: list[str]) -> dict[str, Any]:
            tool = runtime.tool_registry.get(tool_name)
            metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
            return asyncio.run(
                _audited_runtime_tool_call(
                    runtime,
                    tool_name,
                    dict(payload or {}),
                    headers=self.headers,
                    metadata=metadata,
                    source_chain=source_chain,
                )
            )

        def do_GET(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query = parse_qs(parsed_url.query)
            # WebSocket 仅 ASGI 模式可用;legacy-http 下优雅降级,避免前端无限重连 404
            if path == "/ws" or self.headers.get("upgrade", "").lower() == "websocket":
                self._send_json(
                    426,
                    {
                        "error": "WebSocket requires ASGI mode",
                        "detail": "Restart aiask-agent without --legacy-http to enable /ws",
                        "object": "aiask.ws_unavailable",
                    },
                )
                return
            if path == "/health":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "aiask-agent",
                        "host": self.server.server_address[0],
                        "port": self.server.server_address[1],
                    },
                )
                return
            if path == "/health/detailed":
                parity_names = (
                    _build_native_full_runtime().tool_registry.names()
                    if _hermes_full_enabled()
                    else runtime.tool_registry.names()
                )
                parity = parity_summary(parity_names, env=dict(os.environ), gateway_adapters=ADAPTERS.keys())
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "aiask-agent",
                        "host": self.server.server_address[0],
                        "port": self.server.server_address[1],
                        "runtime": {
                            "model": runtime.model,
                            "max_iterations": runtime.max_iterations,
                            "model_timeout_seconds": runtime.model_timeout_seconds,
                            "tool_timeout_seconds": runtime.tool_timeout_seconds,
                        },
                        "tools": {
                            "count": len(runtime.tool_registry.names()),
                            "names": runtime.tool_registry.names(),
                            "toolset": runtime.tool_registry.policy_engine.toolset,
                        },
                        "hermes": {
                            "mode": "aiask_native",
                            "full_mode_enabled": _hermes_full_enabled(),
                            "full_mode_active": full_runtime_manager.active(),
                            "parity": _redact_required_env(
                                parity,
                                redact_sensitive_names=True,
                            ),
                            "live_evidence": _redact_required_env(_parity_live_evidence(parity), redact_sensitive_names=True),
                        },
                        "control": {
                            "loopback_only": True,
                            "token_configured": bool(
                                str(os.getenv("AIASK_AGENT_CONTROL_TOKEN", "")).strip()
                                or str(os.getenv("AIASK_LOCAL_CONTROL_TOKEN", "")).strip()
                            ),
                        },
                    },
                )
                return
            if path == "/v1/hermes/status":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(
                    200,
                    {
                        "object": "aiask.hermes_status",
                        "implementation": "aiask_native",
                        "baseline": HERMES_BASELINE,
                        "baseline_version": HERMES_BASELINE_VERSION,
                        "baseline_release_tag": HERMES_RELEASE_TAG,
                        "embedded_vendor_runtime": False,
                        "full_mode_enabled": _hermes_full_enabled(),
                        "full_mode_active": full_runtime_manager.active(),
                        "parity": parity_summary(
                            full_runtime_manager.current().tool_registry.names() if full_runtime_manager.active() else runtime.tool_registry.names(),
                            env=dict(os.environ),
                            gateway_adapters=ADAPTERS.keys(),
                        ),
                        "providers": ModelProviderRegistry(usage_store=ProviderUsageStore(runtime.session_store.path)).status(),
                        "memory": MemoryProviderManager(path=runtime.session_store.path).status(),
                        "acp": ACPManager(mcp=MCPAggregator()).status(),
                        "security": SecurityScanner(policy=runtime.tool_registry.policy_engine.policy).status(),
                        "skill_packs": SkillPackManager(skill_store=SkillStore()).status(),
                    },
                )
                return
            if path == "/v1/capabilities/parity":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                selected = _build_native_full_runtime() if _hermes_full_enabled() else runtime
                self._send_json(200, parity_summary(selected.tool_registry.names(), env=dict(os.environ), gateway_adapters=ADAPTERS.keys()))
                return
            if path == "/v1/hermes/toolsets":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(
                    200,
                    {
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
                    },
                )
                return
            if path == "/v1/hermes/config":
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                full = _build_native_full_runtime()
                self._send_json(
                    200,
                    {
                        "object": "aiask.hermes_config",
                        "home": os.getenv("AIASK_AGENT_HOME", ""),
                        "toolset": full.tool_registry.policy_engine.toolset,
                        "workspace_roots": list(full.tool_registry.policy_engine.policy.workspace_roots),
                        "secrets_redacted": True,
                    },
                )
                return
            if path == "/v1/hermes/tools":
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                full = _build_native_full_runtime()
                self._send_json(200, build_tool_catalog_payload(full, implementation="aiask_native"))
                return
            if path == "/v1/hermes/handoffs":
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                self._send_json(
                    200,
                    _handoff_queue_payload(
                        runtime,
                        user_id=(query.get("user_id") or [None])[0],
                        session_id=(query.get("session_id") or [None])[0],
                        status=(query.get("status") or [None])[0],
                        limit=int((query.get("limit") or ["100"])[0]),
                        include_completed=_query_bool(query, "include_completed"),
                    ),
                )
                return
            if path.startswith("/v1/hermes/sessions/") and path.endswith("/resume-context"):
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                parts = path.strip("/").split("/")
                session_id = parts[3] if len(parts) >= 4 else ""
                try:
                    self._send_json(200, _session_resume_context_payload(runtime, session_id, intent_store=intent_executor.store))
                except FileNotFoundError as exc:
                    self._send_error_json(404, str(exc), code="not_found")
                return
            if path == "/v1/hermes/sessions":
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "implementation": "aiask_native",
                        "data": _session_summary_payload(
                            runtime,
                            intent_store=intent_executor.store,
                            user_id=(query.get("user_id") or [None])[0],
                            limit=int((query.get("limit") or ["100"])[0]),
                            include_archived=_query_bool(query, "include_archived"),
                        ),
                        "include_archived": _query_bool(query, "include_archived"),
                    },
                )
                return
            if path == "/v1/tools":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, build_tool_catalog_payload(runtime))
                return
            if path == "/v1/desktop/settings/status":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                control_ok, control_reason = self._control_authorized()
                payload = _desktop_settings_status_payload_for_runtime(
                    runtime,
                    endpoint=f"http://{self.server.server_address[0]}:{self.server.server_address[1]}",
                    control_authorized=control_ok,
                    control_reason=control_reason,
                )
                self._send_json(200, payload)
                return
            if path == "/v1/desktop/data/status":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                codes = (query.get("codes") or [""])[0]
                payload = asyncio.run(
                    _desktop_data_status_payload_for_runtime(
                        runtime,
                        {
                            "codes": [item.strip() for item in str(codes or "").replace("\n", ",").split(",") if item.strip()],
                            "max_stale_days": int((query.get("max_stale_days") or ["5"])[0]),
                        }
                    )
                )
                self._send_json(200, payload)
                return
            if path == "/v1/desktop/stock-data-sources":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, list_stock_data_sources())
                return
            if path == "/v1/desktop/users/local-profile":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, local_profile_payload())
                return
            if path.startswith("/v1/desktop/users/") and path.endswith("/activity"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                user_id = parts[3] if len(parts) >= 5 else ""
                ok, scoped_user_id, status, reason = self._require_user_scope(user_id)
                if not ok:
                    self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                    return
                self._send_json(
                    200,
                    runtime.session_store.user_activity_summary(
                        user_id=scoped_user_id,
                        limit=int((query.get("limit") or ["20"])[0]),
                    ),
                )
                return
            if path == "/v1/desktop/analytics/summary":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                requested_user_id = (query.get("user_id") or [""])[0].strip() or None
                scoped_user_id = None
                if requested_user_id:
                    ok, scoped_user_id, status, reason = self._require_user_scope(requested_user_id)
                    if not ok:
                        self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                        return
                else:
                    ok, reason = self._control_authorized()
                    if not ok:
                        status = 503 if reason == "control token is not configured" else 401
                        self._send_error_json(status, reason or "control token required", code="control_required")
                        return
                self._send_json(200, runtime.session_store.analytics_summary(user_id=scoped_user_id, limit=int((query.get("limit") or ["20"])[0])))
                return
            if path.startswith("/v1/desktop/users/") and path.endswith("/export"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                user_id = parts[3] if len(parts) >= 5 else ""
                ok, scoped_user_id, status, reason = self._require_user_scope(user_id)
                if not ok:
                    self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                    return
                self._send_json(200, runtime.session_store.export_user_data(user_id=scoped_user_id, limit=int((query.get("limit") or ["500"])[0])))
                return
            if path.startswith("/v1/desktop/users/") and path.endswith("/learning-dataset"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                user_id = parts[3] if len(parts) >= 5 else ""
                ok, scoped_user_id, status, reason = self._require_user_scope(user_id)
                if not ok:
                    self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                    return
                self._send_json(200, runtime.session_store.learning_dataset(user_id=scoped_user_id, limit=int((query.get("limit") or ["100"])[0])))
                return
            if path.startswith("/v1/desktop/users/") and path.endswith("/recommendations"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                user_id = parts[3] if len(parts) >= 5 else ""
                ok, scoped_user_id, status, reason = self._require_user_scope(user_id)
                if not ok:
                    self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                    return
                self._send_json(200, runtime.session_store.workflow_recommendations(user_id=scoped_user_id, limit=int((query.get("limit") or ["5"])[0])))
                return
            if path.startswith("/v1/desktop/users/") and path.endswith("/data-policy"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                user_id = parts[3] if len(parts) >= 5 else ""
                ok, scoped_user_id, status, reason = self._require_user_scope(user_id)
                if not ok:
                    self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                    return
                self._send_json(200, {"object": "aiask.user_data_policy", "data": runtime.session_store.get_user_data_policy(scoped_user_id)})
                return
            if path == "/v1/desktop/factor-factory/status":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, asyncio.run(factor_factory_status(limit=int((query.get("limit") or ["50"])[0]))))
                return
            if path == "/v1/desktop/trade-predictions/status":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(
                    200,
                    asyncio.run(
                        runtime.tool_registry.call_tool(
                            "agent_trade_prediction_status",
                            {
                                "strategy_id": (query.get("strategy_id") or [None])[0],
                                "stock_code": (query.get("stock_code") or [None])[0],
                                "limit": int((query.get("limit") or ["1000"])[0]),
                            },
                        )
                    ),
                )
                return
            if path == "/v1/desktop/trade-predictions/outcomes":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(
                    200,
                    asyncio.run(
                        runtime.tool_registry.call_tool(
                            "agent_trade_prediction_outcomes",
                            {
                                "prediction_id": (query.get("prediction_id") or [None])[0],
                                "strategy_id": (query.get("strategy_id") or [None])[0],
                                "stock_code": (query.get("stock_code") or [None])[0],
                                "score_version": (query.get("score_version") or [None])[0],
                                "score_status": (query.get("score_status") or [None])[0],
                                "data_quality_status": (query.get("data_quality_status") or [None])[0],
                                "actual_trading_date_lte": (query.get("actual_trading_date_lte") or [None])[0],
                                "actual_trading_date_gte": (query.get("actual_trading_date_gte") or [None])[0],
                                "limit": int((query.get("limit") or ["100"])[0]),
                            },
                        )
                    ),
                )
                return
            if path == "/v1/desktop/trade-predictions/matrix":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                dimensions = [
                    item.strip()
                    for item in str((query.get("dimensions") or [""])[0] or "").split(",")
                    if item.strip()
                ]
                self._send_json(
                    200,
                    asyncio.run(
                        runtime.tool_registry.call_tool(
                            "agent_trade_prediction_matrix",
                            {
                                "strategy_id": (query.get("strategy_id") or [None])[0],
                                "stock_code": (query.get("stock_code") or [None])[0],
                                "score_version": (query.get("score_version") or [None])[0],
                                "dimensions": dimensions,
                                "limit": int((query.get("limit") or ["1000"])[0]),
                            },
                        )
                    ),
                )
                return
            if path == "/v1/desktop/stock-radar/status":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(
                    200,
                    asyncio.run(
                        runtime.tool_registry.call_tool(
                            "agent_stock_radar_status",
                            {
                                "run_id": (query.get("run_id") or [None])[0],
                                "limit": int((query.get("limit") or ["20"])[0]),
                            },
                        )
                    ),
                )
                return
            if path == "/v1/desktop/stock-radar/candidates":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                min_score_raw = (query.get("min_score") or [None])[0]
                self._send_json(
                    200,
                    asyncio.run(
                        runtime.tool_registry.call_tool(
                            "agent_stock_radar_candidates",
                            {
                                "run_id": (query.get("run_id") or [None])[0],
                                "tier": (query.get("tier") or [None])[0],
                                "symbol": (query.get("symbol") or [None])[0],
                                "min_score": float(min_score_raw) if min_score_raw not in {None, ""} else None,
                                "limit": int((query.get("limit") or ["100"])[0]),
                            },
                        )
                    ),
                )
                return
            if path == "/v1/desktop/stock-radar/digest":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                channels = [
                    item.strip()
                    for item in str((query.get("channels") or ["wecom,telegram"])[0]).split(",")
                    if item.strip()
                ]
                self._send_json(
                    200,
                    asyncio.run(
                        runtime.tool_registry.call_tool(
                            "agent_stock_radar_digest",
                            {
                                "run_id": (query.get("run_id") or [None])[0],
                                "limit": int((query.get("limit") or ["20"])[0]),
                                "channels": channels,
                            },
                        )
                    ),
                )
                return
            if path == "/v1/desktop/workbench/summary":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(
                    200,
                    _workbench_summary_payload(
                        runtime,
                        intent_store=intent_executor.store,
                        user_id=(query.get("user_id") or [None])[0],
                        session_limit=int((query.get("session_limit") or ["8"])[0]),
                        run_limit=int((query.get("run_limit") or ["8"])[0]),
                    ),
                )
                return
            if path == "/v1/desktop/runs":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(
                    200,
                    _desktop_runs_payload(
                        runtime,
                        session_id=(query.get("session_id") or [None])[0],
                        status=(query.get("status") or [None])[0],
                        limit=int((query.get("limit") or ["100"])[0]),
                    ),
                )
                return
            if path == "/v1/ai/status":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, _ai_status_payload_for_runtime(runtime))
                return
            if path == "/v1/ai/config":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, _ai_config_payload_for_runtime(runtime))
                return
            if path == "/v1/ai/models":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, asyncio.run(_ai_models_payload_for_runtime(runtime)))
                return
            if path == "/v1/desktop/quant/presets":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, quant_adapter.quant_presets())
                return
            if path == "/v1/desktop/financial-manager/catalog":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, _financial_catalog_payload(runtime))
                return
            if path == "/v1/desktop/financial-manager/status":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, asyncio.run(_financial_status_payload(runtime, intent_store=intent_executor.store)))
                return
            if path == "/v1/desktop/broker-readiness":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, _broker_readiness_payload(runtime))
                return
            if path in {"/v1/desktop/broker/accounts", "/v1/desktop/broker/positions", "/v1/desktop/broker/orders"}:
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                requested_user_id = (query.get("user_id") or [""])[0].strip() or None
                scoped_user_id = None
                if requested_user_id:
                    ok, scoped_user_id, status, reason = self._require_user_scope(requested_user_id)
                    if not ok:
                        self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                        return
                self._send_json(200, _broker_accounts_payload(runtime, {"user_id": scoped_user_id, "provider": (query.get("provider") or [""])[0]}))
                return
            if path == "/v1/desktop/broker/analytics/latest":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                requested_user_id = (query.get("user_id") or [""])[0].strip() or None
                scoped_user_id = None
                if requested_user_id:
                    ok, scoped_user_id, status, reason = self._require_user_scope(requested_user_id)
                    if not ok:
                        self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                        return
                latest = runtime.session_store.latest_broker_analytics(
                    user_id=scoped_user_id,
                    provider=normalize_provider((query.get("provider") or [""])[0]) if (query.get("provider") or [""])[0] else None,
                )
                self._send_json(
                    200,
                    {
                        "object": "aiask.desktop.broker_readonly.analytics",
                        "success": True,
                        "data": {"analytics": latest},
                        "error": None,
                        "read_only": True,
                        "live_trading_enabled": False,
                        "secrets_redacted": True,
                        "source_chain": ["aiask_agent.broker_readonly"],
                    },
                )
                return
            if path.startswith("/v1/desktop/quant/research-runs/"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                research_id = parts[4] if len(parts) >= 5 else ""
                if path.endswith("/report"):
                    report = quant_store.report(research_id)
                    if report is None:
                        self._send_error_json(404, f"quant research report not found: {research_id}", code="not_found")
                        return
                    self._send_json(200, report)
                    return
                item = quant_store.get(research_id)
                if item is None:
                    self._send_error_json(404, f"quant research run not found: {research_id}", code="not_found")
                    return
                self._send_json(200, {"object": "aiask.quant_research_run", **item})
                return
            if path == "/v1/toolsets":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                current = runtime.tool_registry.policy_engine.policy
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "active": current.toolset,
                        "general_tools_enabled": current.general_tools_enabled,
                        "workspace_roots": list(current.workspace_roots),
                        "data": [
                            {"name": "finance_safe", "default": current.toolset == "finance_safe"},
                            {
                                "name": GENERAL_FULL_TOOLSET,
                                "enabled": current.toolset == GENERAL_FULL_TOOLSET and current.general_tools_enabled,
                            },
                        ],
                    },
                )
                return
            if path == "/v1/mcp/servers":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                include_all = _query_bool(query, "all")
                self._send_json(200, {"object": "list", "data": MCPAggregator().servers_summary(include_all=include_all)})
                return
            if path in {"/v1/mcp/tools", "/v1/mcp/resources", "/v1/mcp/prompts", "/v1/mcp/oauth_status"}:
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                include_all = _query_bool(query, "all")
                mcp = MCPAggregator()
                if path == "/v1/mcp/tools":
                    data = mcp.tools_summary(include_all=include_all)
                elif path == "/v1/mcp/resources":
                    data = mcp.resources_summary(include_all=include_all)
                elif path == "/v1/mcp/prompts":
                    data = mcp.prompts_summary(include_all=include_all)
                else:
                    data = mcp.oauth_status(include_all=include_all)
                self._send_json(200, {"object": "list", "data": data})
                return
            if path == "/v1/search":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                q = (query.get("query") or query.get("q") or [""])[0]
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "include_archived": _query_bool(query, "include_archived"),
                        "data": runtime.session_store.search(
                            query=q,
                            session_id=(query.get("session_id") or [None])[0],
                            user_id=(query.get("user_id") or [None])[0],
                            limit=int((query.get("limit") or ["20"])[0]),
                            include_archived=_query_bool(query, "include_archived"),
                        ),
                    },
                )
                return
            if path.startswith("/v1/artifacts/"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                if path.endswith("/content"):
                    parts = path.strip("/").split("/")
                    artifact_id = parts[2] if len(parts) >= 3 else ""
                    try:
                        self._send_json(
                            200,
                            _artifact_content_payload(
                                runtime.session_store,
                                artifact_id,
                                max_bytes=int((query.get("max_bytes") or ["262144"])[0]),
                            ),
                        )
                    except FileNotFoundError as exc:
                        self._send_error_json(404, str(exc), code="not_found")
                    return
                artifact_id = path.rsplit("/", 1)[-1].strip()
                item = runtime.session_store.get_artifact(artifact_id)
                if item is None:
                    self._send_error_json(404, f"artifact not found: {artifact_id}", code="not_found")
                    return
                self._send_json(200, {"object": "artifact", **item})
                return
            if path.startswith("/v1/sources/"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                source_id = path.rsplit("/", 1)[-1].strip()
                item = runtime.session_store.get_source(source_id)
                if item is None:
                    self._send_error_json(404, f"source not found: {source_id}", code="not_found")
                    return
                self._send_json(200, {"object": "source", **item})
                return
            if path.startswith("/v1/tool-invocations/"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                invocation_id = path.rsplit("/", 1)[-1].strip()
                item = runtime.session_store.get_tool_invocation(invocation_id)
                if item is None:
                    self._send_error_json(404, f"tool invocation not found: {invocation_id}", code="not_found")
                    return
                self._send_json(200, {"object": "tool_invocation", **item})
                return
            if path.startswith("/v1/sessions/") and path.endswith("/artifacts"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                session_id = parts[2] if len(parts) >= 3 else ""
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "session_id": session_id,
                        "data": runtime.session_store.list_artifacts(
                            session_id=session_id,
                            kind=(query.get("kind") or [None])[0],
                            limit=int((query.get("limit") or ["100"])[0]),
                        ),
                    },
                )
                return
            if path.startswith("/v1/sessions/") and path.endswith("/sources"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                session_id = parts[2] if len(parts) >= 3 else ""
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "session_id": session_id,
                        "data": runtime.session_store.list_sources(
                            session_id=session_id,
                            source_type=(query.get("source_type") or [None])[0],
                            limit=int((query.get("limit") or ["100"])[0]),
                        ),
                    },
                )
                return
            if path.startswith("/v1/sessions/") and path.endswith("/messages"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                parts = path.strip("/").split("/")
                session_id = parts[2] if len(parts) >= 3 else ""
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "session_id": session_id,
                        "data": runtime.session_store.list_session_messages(
                            session_id,
                            limit=int((query.get("limit") or ["200"])[0]),
                        ),
                    },
                )
                return
            if path.startswith("/v1/runs/") and (path.endswith("/events") or path.endswith("/events/stream")):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                run_id = path.strip("/").split("/")[2]
                last_id = int(self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0] or 0)
                events = runtime.session_store.list_run_events(run_id, after_event_id=last_id)
                self._send_sse(events)
                return
            if path.startswith("/v1/runs/") and path.endswith("/artifacts"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                run_id = path.strip("/").split("/")[2]
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "run_id": run_id,
                        "data": runtime.session_store.list_artifacts(
                            run_id=run_id,
                            kind=(query.get("kind") or [None])[0],
                            limit=int((query.get("limit") or ["100"])[0]),
                        ),
                    },
                )
                return
            if path.startswith("/v1/runs/") and path.endswith("/sources"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                run_id = path.strip("/").split("/")[2]
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "run_id": run_id,
                        "data": runtime.session_store.list_sources(
                            run_id=run_id,
                            source_type=(query.get("source_type") or [None])[0],
                            limit=int((query.get("limit") or ["100"])[0]),
                        ),
                    },
                )
                return
            if path.startswith("/v1/runs/") and path.endswith("/trace-eval"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                run_id = path.strip("/").split("/")[2]
                try:
                    self._send_json(200, _run_trace_eval_payload(runtime, run_id))
                except FileNotFoundError as exc:
                    self._send_error_json(404, str(exc), code="not_found")
                return
            if path.startswith("/v1/runs/") and path.endswith("/tool-invocations"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                run_id = path.strip("/").split("/")[2]
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "run_id": run_id,
                        "data": runtime.session_store.list_tool_invocations(
                            run_id=run_id,
                            limit=int((query.get("limit") or ["100"])[0]),
                        ),
                    },
                )
                return
            if path.startswith("/v1/runs/"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                run_id = path.rsplit("/", 1)[-1].strip()
                item = runtime.session_store.get_run(run_id)
                if item is None:
                    self._send_error_json(404, f"run not found: {run_id}", code="not_found")
                    return
                self._send_json(200, {"object": "run", **item})
                return
            if path == "/v1/jobs":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, {"object": "list", "data": runtime.job_store.list()})
                return
            if path.startswith("/v1/jobs/") and path.endswith("/runs"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                job_id = path.strip("/").split("/")[2]
                limit = int((query.get("limit") or ["100"])[0])
                self._send_json(200, {"object": "list", "job_id": job_id, "data": runtime.job_store.list_runs(job_id, limit=limit)})
                return
            if path == "/intents":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "data": public_intents(
                            intent_executor.store.list(
                                status=(query.get("status") or [None])[0],
                                limit=int((query.get("limit") or ["100"])[0]),
                            )
                        ),
                        "secrets_redacted": True,
                    },
                )
                return
            if path.startswith("/intents/"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                intent_id = path.split("/")[2] if len(path.split("/")) > 2 else ""
                result = asyncio.run(
                    runtime.tool_registry.call_tool("agent_action_intent_get", {"intent_id": intent_id})
                )
                self._send_json(200 if result.get("success") else 404, result)
                return
            if path.startswith("/v1/responses/"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                response_id = path.rsplit("/", 1)[-1].strip()
                payload = runtime.session_store.get_response(response_id)
                if payload is None:
                    self._send_error_json(404, f"response not found: {response_id}", code="not_found")
                    return
                self._send_json(200, {"object": "response", **payload})
                return
            self._send_error_json(404, "not found", code="not_found")

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            if path.startswith("/v1/jobs/"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                job_id = path.rsplit("/", 1)[-1].strip()
                deleted = runtime.job_store.delete(job_id)
                self._send_json(200, {"id": job_id, "object": "job.deleted", "deleted": deleted})
                return
            if path.startswith("/v1/responses/"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                response_id = path.rsplit("/", 1)[-1].strip()
                deleted = runtime.session_store.delete_response(response_id)
                self._send_json(200, {"id": response_id, "object": "response.deleted", "deleted": deleted})
                return
            if not self._api_authorized():
                self._send_error_json(401, "unauthorized", code="unauthorized")
                return
            self._send_error_json(404, "not found", code="not_found")

        def do_PATCH(self) -> None:
            path = urlparse(self.path).path
            try:
                payload = _read_json(self)
            except ValueError as exc:
                self._send_error_json(400, str(exc), code="invalid_request")
                return
            if path == "/v1/ai/config":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                try:
                    self._send_json(200, asyncio.run(_save_ai_config_for_runtime(runtime, payload)))
                except ValueError as exc:
                    self._send_error_json(400, str(exc), code="invalid_request")
                return
            if path.startswith("/v1/jobs/"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                job_id = path.rsplit("/", 1)[-1].strip()
                job = runtime.job_store.update(job_id, **payload)
                if not job:
                    self._send_error_json(404, f"job not found: {job_id}", code="not_found")
                    return
                self._send_json(200, {"object": "job", **job})
                return
            if path == "/v1/desktop/users/local-profile":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                profile = save_local_profile(payload)
                self._send_json(200, profile)
                return
            if path.startswith("/v1/desktop/users/") and path.endswith("/data-policy"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                parts = path.strip("/").split("/")
                user_id = parts[3] if len(parts) >= 5 else ""
                ok, scoped_user_id, status, reason = self._require_user_scope(user_id)
                if not ok:
                    self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                    return
                self._send_json(
                    200,
                    {
                        "object": "aiask.user_data_policy",
                        "data": runtime.session_store.update_user_data_policy(scoped_user_id, payload),
                    },
                )
                return
            if not self._api_authorized():
                self._send_error_json(401, "unauthorized", code="unauthorized")
                return
            self._send_error_json(404, "not found", code="not_found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                payload = _read_json(self)
            except ValueError as exc:
                self._send_error_json(400, str(exc), code="invalid_request")
                return

            if path == "/v1/chat/completions":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                messages = payload.get("messages")
                if not isinstance(messages, list):
                    self._send_error_json(400, "messages must be an array", code="invalid_request")
                    return
                selected_runtime, mode, auth = self._mode_runtime(payload)
                if not auth[0] or selected_runtime is None:
                    status = 503 if auth[1] in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, auth[1] or "unauthorized", code="mode_unauthorized")
                    return
                model = str(payload.get("model") or selected_runtime.model)
                try:
                    result = asyncio.run(
                        selected_runtime.run(
                            [dict(item) for item in messages if isinstance(item, dict)],
                            session_id=payload.get("session_id") or self.headers.get("X-AIASK-Session-Id"),
                            user_id=payload.get("user_id") or self.headers.get("X-AIASK-User-Id"),
                            stream=bool(payload.get("stream", False)),
                        )
                    )
                except Exception as exc:
                    self._send_error_json(500, str(exc), code="agent_error")
                    return
                if bool(payload.get("stream", False)):
                    self._send_chat_completion_sse(result, model=model)
                    return
                response_payload = _chat_completion_payload(result, model=model)
                response_payload["aiask"]["mode"] = mode
                self._send_json(200, response_payload)
                return

            if path == "/v1/responses":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                messages = _messages_from_responses_payload(payload)
                selected_runtime, mode, auth = self._mode_runtime(payload)
                if not auth[0] or selected_runtime is None:
                    status = 503 if auth[1] in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, auth[1] or "unauthorized", code="mode_unauthorized")
                    return
                model = str(payload.get("model") or selected_runtime.model)
                try:
                    result = asyncio.run(
                        selected_runtime.run(
                            messages,
                            session_id=payload.get("session_id") or self.headers.get("X-AIASK-Session-Id"),
                            user_id=payload.get("user_id") or self.headers.get("X-AIASK-User-Id"),
                            stream=bool(payload.get("stream", False)),
                        )
                    )
                except Exception as exc:
                    self._send_error_json(500, str(exc), code="agent_error")
                    return
                if bool(payload.get("stream", False)):
                    self._send_response_sse(result, model=model)
                    return
                response_payload = _responses_payload(result, model=model)
                response_payload["metadata"]["mode"] = mode
                self._send_json(200, response_payload)
                return

            if path.startswith("/v1/sessions/") and path.endswith("/undo"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                parts = path.strip("/").split("/")
                session_id = parts[2] if len(parts) >= 3 else ""
                result = runtime.session_store.undo_last_turns(
                    session_id,
                    turns=payload.get("turns") or 1,
                    reason=str(payload.get("reason") or "hermes_undo"),
                    deleted_by=str(payload.get("deleted_by") or payload.get("user_id") or "control_token"),
                )
                self._send_json(200, {"object": "aiask.session_undo", "implementation": "aiask_native", **result})
                return

            if path.startswith("/v1/sessions/") and path.endswith("/archive"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                parts = path.strip("/").split("/")
                session_id = parts[2] if len(parts) >= 3 else ""
                try:
                    result = runtime.session_store.set_session_archived(
                        session_id,
                        archived=_truthy(payload.get("archived", True)),
                        reason=str(payload.get("reason") or "desktop archive"),
                        actor=str(payload.get("actor") or payload.get("user_id") or "control_token"),
                    )
                except FileNotFoundError as exc:
                    self._send_error_json(404, str(exc), code="not_found")
                    return
                self._send_json(200, {"object": "aiask.session_archive", "implementation": "aiask_native", **result})
                return

            if path == "/v1/desktop/events":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                events = runtime.session_store.record_activity_events(self._event_batch_from_payload(payload))
                self._send_json(200, {"object": "list", "data": events, "count": len(events), "secrets_redacted": True})
                return

            if path == "/v1/desktop/feedback":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                context = _request_context_payload(payload, headers=self.headers)
                feedback = runtime.session_store.record_feedback({**context, **payload})
                self._send_json(200, {"object": "aiask.feedback", "data": feedback, "secrets_redacted": True})
                return

            if path.startswith("/v1/desktop/users/") and path.endswith("/delete"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                parts = path.strip("/").split("/")
                user_id = parts[3] if len(parts) >= 5 else ""
                ok, scoped_user_id, status, reason = self._require_user_scope(user_id)
                if not ok:
                    self._send_error_json(status, reason or "unauthorized", code="user_scope_forbidden")
                    return
                self._send_json(
                    200,
                    runtime.session_store.delete_user_data(
                        user_id=scoped_user_id,
                        include_conversations=payload.get("include_conversations") is not False,
                        include_audit=payload.get("include_audit") is not False,
                        hard_delete=_truthy(payload.get("hard_delete")),
                        dry_run=payload.get("dry_run") is not False,
                        reason=str(payload.get("reason") or "user_data_delete"),
                        actor=str(payload.get("actor") or scoped_user_id),
                    ),
                )
                return

            if path == "/v1/desktop/retention/sweep":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "control token required", code="control_required")
                    return
                user_id = str(payload.get("user_id") or "").strip() or None
                self._send_json(200, runtime.session_store.apply_retention_policies(user_id=user_id, dry_run=payload.get("dry_run") is not False))
                return

            if path.startswith("/v1/tools/"):
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                tool_name = path.rsplit("/", 1)[-1].strip()
                tool = runtime.tool_registry.get(tool_name)
                metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
                if not _metadata_allows_read_only_desktop_call(metadata, tool_name) and not _is_read_only_desktop_tool(tool_name):
                    self._send_error_json(
                        403,
                        f"tool is not available through the read-only desktop API: {tool_name}",
                        code="tool_forbidden",
                    )
                    return
                result = self._audited_tool_call(
                    runtime,
                    tool_name,
                    payload,
                    metadata=metadata,
                    source_chain=["aiask_agent.server", "desktop.read_only_tool"],
                    result_guard=read_only_desktop_result_guard,
                )
                self._send_json(200, result)
                return

            if path == "/v1/desktop/quant/research-runs":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                result = self._audited_desktop_tool_call(
                    "agent_quant_research_run",
                    payload,
                    source_chain=["aiask_agent.server", "desktop.quant_research"],
                )
                self._send_json(200, result)
                return

            if path == "/v1/desktop/financial-manager/query":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    tool = runtime.tool_registry.get(tool_name)
                    metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
                    return await _audited_runtime_tool_call(
                        runtime,
                        tool_name,
                        dict(arguments or {}),
                        headers=self.headers,
                        metadata=metadata,
                        source_chain=["aiask_agent.server", "desktop.financial_manager"],
                        result_guard=read_only_desktop_result_guard,
                    )

                self._send_json(200, asyncio.run(_financial_query_payload(runtime, payload, tool_caller=call_tool)))
                return

            if path == "/v1/desktop/financial-manager/intent":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    tool = runtime.tool_registry.get(tool_name)
                    metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
                    return await _audited_runtime_tool_call(
                        runtime,
                        tool_name,
                        dict(arguments or {}),
                        headers=self.headers,
                        metadata=metadata,
                        source_chain=["aiask_agent.server", "desktop.financial_manager.intent"],
                    )

                self._send_json(200, asyncio.run(_financial_intent_payload(runtime, payload, tool_caller=call_tool)))
                return

            if path == "/v1/desktop/broker/sync":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    tool = runtime.tool_registry.get(tool_name)
                    metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
                    return await _audited_runtime_tool_call(
                        runtime,
                        tool_name,
                        dict(arguments or {}),
                        headers=self.headers,
                        metadata=metadata,
                        source_chain=["aiask_agent.server", "desktop.broker_readonly"],
                        result_guard=read_only_desktop_result_guard,
                    )

                self._send_json(200, asyncio.run(_broker_sync_payload(runtime, payload, headers=self.headers, tool_caller=call_tool)))
                return

            if path == "/v1/desktop/broker/analytics/run":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                self._send_json(200, _broker_analytics_payload(runtime, payload))
                return

            if path == "/v1/desktop/data/sync-plan":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, asyncio.run(_desktop_data_sync_plan_payload_for_runtime(runtime, payload)))
                return

            if path == "/v1/desktop/stock-data-sources":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                try:
                    self._send_json(200, save_stock_data_source(payload))
                except ValueError as exc:
                    self._send_error_json(400, str(exc), code="invalid_request")
                return

            if path == "/v1/desktop/stock-data-sources/test":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                self._send_json(200, test_stock_data_source(payload))
                return

            if path == "/v1/ai/smoke":
                if not self._api_authorized():
                    self._send_error_json(401, "unauthorized", code="unauthorized")
                    return
                self._send_json(200, asyncio.run(_ai_smoke_payload_for_runtime(runtime, payload)))
                return

            if path == "/v1/desktop/users/local-profile":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                self._send_json(200, save_local_profile(payload))
                return

            if path == "/v1/jobs":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                try:
                    job = runtime.job_store.create(
                        name=str(payload.get("name") or ""),
                        prompt=str(payload.get("prompt") or ""),
                        schedule=payload.get("schedule"),
                        interval_seconds=payload.get("interval_seconds"),
                        toolset=str(payload.get("toolset") or "finance_safe"),
                        enabled=bool(payload.get("enabled", True)),
                        payload={key: payload.get(key) for key in ("script", "skills", "silent_pattern") if key in payload},
                    )
                except Exception as exc:
                    self._send_error_json(400, str(exc), code="invalid_request")
                    return
                self._send_json(201, {"object": "job", **job})
                return

            if path == "/intents":
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                result = self._audited_desktop_tool_call(
                    "agent_action_intent_create",
                    payload,
                    source_chain=["aiask_agent.server", "intent.api"],
                )
                self._send_json(200 if result.get("success") else 400, result)
                return

            if path.startswith("/v1/hermes/admin/tools/"):
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                tool_name = path.rsplit("/", 1)[-1].strip()
                selected = _build_native_full_runtime()
                tool = selected.tool_registry.get(tool_name)
                metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
                result = self._audited_tool_call(
                    selected,
                    tool_name,
                    payload,
                    metadata=metadata,
                    source_chain=["aiask_agent.server", "hermes.admin_tool"],
                )
                status = 200 if result.get("success") or result.get("error_code") == "ACTION_INTENT_REQUIRED" else 400
                self._send_json(status, result)
                return

            if path.startswith("/v1/jobs/") and path.endswith("/run"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                job_id = path.strip("/").split("/")[2]
                result = asyncio.run(runtime.scheduler.run_job(job_id))
                self._send_json(200 if result.get("success") else 404, result)
                return

            if path.startswith("/v1/plugins/") and path.endswith("/test"):
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                parts = path.strip("/").split("/")
                if len(parts) >= 6 and parts[3] == "tools":
                    name = parts[2]
                    tool = parts[4]
                    manager = NativePluginManager()
                    plugin = manager.get(name)
                    if not plugin:
                        self._send_error_json(404, f"plugin not found: {name}", code="not_found")
                        return
                    if str(tool or "").strip().lower() in {"", "__manifest__", "manifest", "self-test", "self_test"}:
                        self._send_json(200, _plugin_self_test_payload(plugin, name))
                        return
                    plugin_name = _safe_slug(str(plugin.get("name") or name)).replace("-", "_")
                    wrapped = f"agent_plugin_{plugin_name}_{_safe_slug(tool).replace('-', '_')}"
                    collisions = [
                        dict(definition)
                        for item in list(manager.tool_name_collisions() or [])
                        if str(item.get("name") or "") == wrapped
                        for definition in list(item.get("definitions") or [])
                    ]
                    if collisions:
                        self._send_json(
                            200,
                            _public_plugin_test_payload(
                                object_name="plugin.tool_test",
                                success=False,
                                data={
                                    "plugin": str(plugin.get("name") or name),
                                    "tool": tool,
                                    "wrapped_name": wrapped,
                                    "collisions": collisions,
                                    "configured": False,
                                },
                                error=f"plugin tool wrapper name collision for {wrapped}",
                                error_code="PLUGIN_TOOL_NAME_COLLISION",
                            ),
                        )
                        return
                    full = _build_native_full_runtime()
                    if full.tool_registry.get(wrapped) is None:
                        self._send_json(
                            200,
                            _public_plugin_test_payload(
                                object_name="plugin.tool_test",
                                success=False,
                                data={
                                    "plugin": str(plugin.get("name") or name),
                                    "tool": tool,
                                    "available_tools": [str(item.get("name") or "") for item in _plugin_tools(plugin)],
                                    "configured": False,
                                },
                                error=f"plugin tool is not configured: {wrapped}",
                                error_code="PLUGIN_TOOL_NOT_CONFIGURED",
                            ),
                        )
                        return
                    result = asyncio.run(full.tool_registry.call_tool(wrapped, payload))
                    self._send_json(
                        200,
                        _public_plugin_test_payload(
                            object_name="plugin.tool_test",
                            success=result.get("success") is not False,
                            data=result.get("data"),
                            error=result.get("error"),
                            error_code=result.get("error_code"),
                        ),
                    )
                    return
                if len(parts) >= 6 and parts[3] == "commands":
                    name = parts[2]
                    command = parts[4]
                    manager = NativePluginManager()
                    plugin = manager.get(name)
                    if not plugin:
                        self._send_json(
                            200,
                            _public_plugin_test_payload(
                                object_name="plugin.command_test",
                                success=False,
                                data={"plugin": name, "command": command, "configured": False},
                                error=f"plugin not found: {name}",
                                error_code="PLUGIN_COMMAND_NOT_CONFIGURED",
                            ),
                        )
                        return
                    plugin_name = _safe_slug(str(plugin.get("name") or name)).replace("-", "_")
                    wrapped = f"agent_plugin_{plugin_name}_command_{_safe_slug(command).replace('-', '_')}"
                    collisions = [
                        dict(definition)
                        for item in list(manager.tool_name_collisions() or [])
                        if str(item.get("name") or "") == wrapped
                        for definition in list(item.get("definitions") or [])
                    ]
                    if collisions:
                        self._send_json(
                            200,
                            _public_plugin_test_payload(
                                object_name="plugin.command_test",
                                success=False,
                                data={
                                    "plugin": name,
                                    "command": command,
                                    "wrapped_name": wrapped,
                                    "collisions": collisions,
                                    "configured": False,
                                },
                                error=f"plugin command wrapper name collision for {wrapped}",
                                error_code="PLUGIN_COMMAND_NAME_COLLISION",
                            ),
                        )
                        return
                    full = _build_native_full_runtime()
                    if full.tool_registry.get(wrapped) is None:
                        self._send_json(
                            200,
                            _public_plugin_test_payload(
                                object_name="plugin.command_test",
                                success=False,
                                data={"plugin": name, "command": command, "configured": False},
                                error=f"plugin command is not configured: {wrapped}",
                                error_code="PLUGIN_COMMAND_NOT_CONFIGURED",
                            ),
                        )
                        return
                    result = asyncio.run(full.tool_registry.call_tool(wrapped, payload))
                    self._send_json(
                        200,
                        _public_plugin_test_payload(
                            object_name="plugin.command_test",
                            success=result.get("success") is not False,
                            data=result.get("data"),
                            error=result.get("error"),
                            error_code=result.get("error_code"),
                        ),
                    )
                    return

            if path == "/v1/mcp/resources/read":
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                server_name = str(payload.get("server") or "")
                try:
                    self._send_json(
                        200,
                        {
                            "object": "mcp.resource",
                            "success": True,
                            "data": asyncio.run(MCPAggregator().read_resource(server_name, str(payload.get("uri") or ""))),
                            "error": None,
                        },
                    )
                except Exception as exc:
                    self._send_json(200, _mcp_action_error_payload(action="resource", server_name=server_name, exc=exc))
                return

            if path == "/v1/mcp/prompts/get":
                ok, reason = self._hermes_full_authorized()
                if not ok:
                    status = 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401
                    self._send_error_json(status, reason or "unauthorized", code="hermes_full_unauthorized")
                    return
                server_name = str(payload.get("server") or "")
                try:
                    self._send_json(
                        200,
                        {
                            "object": "mcp.prompt",
                            "success": True,
                            "data": asyncio.run(
                                MCPAggregator().get_prompt(
                                    server_name,
                                    str(payload.get("prompt") or payload.get("name") or ""),
                                    dict(payload.get("arguments") or {}),
                                )
                            ),
                            "error": None,
                        },
                    )
                except Exception as exc:
                    self._send_json(200, _mcp_action_error_payload(action="prompt", server_name=server_name, exc=exc))
                return

            if path.startswith("/intents/") and path.endswith("/confirm"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                intent_id = path.split("/")[2]
                result = asyncio.run(intent_executor.confirm(intent_id))
                self._send_json(200 if result.get("success") else 409, result)
                return

            if path.startswith("/intents/") and path.endswith("/deny"):
                ok, reason = self._control_authorized()
                if not ok:
                    status = 503 if reason == "control token is not configured" else 401
                    self._send_error_json(status, reason or "unauthorized", code="control_unauthorized")
                    return
                intent_id = path.split("/")[2]
                result = asyncio.run(intent_executor.deny(intent_id, reason=payload.get("reason")))
                self._send_json(200 if result.get("success") else 409, result)
                return

            self._send_error_json(404, "not found", code="not_found")

    return AIASKAgentHTTPServer((host, int(port)), AIASKAgentHandler)
