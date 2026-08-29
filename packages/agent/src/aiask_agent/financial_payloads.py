from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .ai_payloads import ai_status_payload_for_runtime
from .audited_tool_calls import audited_runtime_tool_call
from .broker_readonly import (
    BrokerSyncContext,
    broker_readiness,
    latest_broker_payload,
    normalize_provider,
    run_behavior_analytics,
    sync_broker_readonly,
)
from .financial_readiness import financial_system_readiness
from .financial_safety import (
    FINANCIAL_MANAGER_ACTIONS,
    FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE,
    FINANCIAL_MANAGER_DRY_RUN_REQUIRED_ERROR,
    FINANCIAL_MANAGER_DRY_RUN_ONLY_ACTIONS,
    FINANCIAL_MANAGER_GROUPS,
    _financial_action_key,
    _financial_action_map,
    _financial_execution_mode,
    financial_safety_matrix,
    normalize_financial_confirmed_action_params,
)
from .mcp_client import MCPAggregator
from .request_context import request_context_payload, tool_payload_with_request_context
from .route_auth import control_token_configured, hermes_full_enabled
from .runtime import AgentRuntime
from .session_store_utils import redact_audit_text
from .tool_registry import aiask_envelope, ensure_aiask_envelope
from .tool_risk import mcp_call_side_effect, mcp_tool_metadata, read_only_desktop_result_guard


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "archived"}
    return bool(value)


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_audit_text(value)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("secret", "token", "api_key", "apikey", "password", "credential")):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _wrapped_mcp_lookup(runtime: AgentRuntime) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for name in runtime.tool_registry.names():
        if not name.startswith("agent_mcp_"):
            continue
        tool = runtime.tool_registry.get(name)
        metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
        side_effect = metadata.get("side_effect") if isinstance(metadata.get("side_effect"), dict) else {}
        target = str(side_effect.get("target") or "")
        for candidate in {name, target, target.split(".")[0]}:
            if candidate:
                lookup.setdefault(candidate, {"wrapped_name": name, "metadata": metadata})
        lowered = name.lower()
        for action in FINANCIAL_MANAGER_ACTIONS:
            raw_tool = str(action.get("mcp_tool") or "").lower()
            if raw_tool and lowered.endswith(raw_tool):
                lookup.setdefault(str(action.get("mcp_tool")), {"wrapped_name": name, "metadata": metadata})
    return lookup


def _internal_financial_mcp_lookup() -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    mcp = MCPAggregator()
    for raw in FINANCIAL_MANAGER_ACTIONS:
        required = str(raw.get("mcp_tool") or "").strip()
        if not required or required in lookup:
            continue
        tool = mcp.internal_financial_tool(required)
        if tool:
            lookup[required] = tool
    return lookup


def _financial_mcp_availability_detail(
    runtime: AgentRuntime,
    action: dict[str, Any],
    mcp_lookup: dict[str, dict[str, Any]],
    *,
    mcp_tools: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    required = str(action.get("mcp_tool") or "").strip()
    if not required:
        return {}
    names = set(runtime.tool_registry.names())
    match = mcp_lookup.get(required)
    internal_match = _internal_financial_mcp_lookup().get(required)
    tools = list(mcp_tools or MCPAggregator().tools_summary(include_all=True))
    servers = list(mcp_servers or MCPAggregator().servers_summary(include_all=True))
    matching_tools = [
        {
            "server": item.get("server"),
            "name": item.get("name"),
            "wrapped_name": item.get("wrapped_name"),
            "domain": item.get("domain"),
        }
        for item in tools
        if str(item.get("name") or "") == required or str(item.get("wrapped_name") or "") == required
    ]
    financial_servers = [
        {
            "name": item.get("name"),
            "domain": item.get("domain"),
            "transport": item.get("transport"),
            "tools_count": len(list(item.get("tools") or [])),
        }
        for item in servers
        if str(item.get("domain") or "").lower() == "financial"
    ]
    reason_code = "agent_mcp_wrapped_tool_ready"
    if internal_match and not match:
        reason_code = "desktop_internal_mcp_tool_ready"
    elif not match:
        if matching_tools:
            reason_code = "mcp_tool_internal_only"
        elif not financial_servers:
            reason_code = "no_financial_mcp_server_registered"
        else:
            reason_code = "mcp_tool_not_discovered"
    wrapped_name = str((match or {}).get("wrapped_name") or "")
    return {
        "required_mcp_tool": required,
        "required_mcp_action": action.get("mcp_action"),
        "reason_code": reason_code,
        "agent_registry_has_wrapped_tool": bool(wrapped_name and wrapped_name in names),
        "desktop_internal_mcp_ready": bool(internal_match),
        "wrapped_tool": wrapped_name or None,
        "internal_tool": internal_match.get("name") if internal_match else None,
        "internal_server": internal_match.get("server") if internal_match else None,
        "matching_configured_tools": matching_tools,
        "financial_servers": financial_servers,
    }


def _financial_tool_availability_detail(runtime: AgentRuntime, action: dict[str, Any]) -> dict[str, Any]:
    required = str(action.get("tool") or "").strip()
    if not required:
        return {}
    names = set(runtime.tool_registry.names())
    return {
        "required_tool": required,
        "reason_code": "agent_tool_ready" if required in names else "agent_tool_missing",
        "agent_registry_has_tool": required in names,
    }


def financial_catalog_payload_for_runtime(runtime: AgentRuntime) -> dict[str, Any]:
    names = set(runtime.tool_registry.names())
    mcp_lookup = _wrapped_mcp_lookup(runtime)
    mcp = MCPAggregator()
    mcp_tools = mcp.tools_summary(include_all=True)
    mcp_servers = mcp.servers_summary(include_all=True)
    safety_matrix = financial_safety_matrix()
    actions: list[dict[str, Any]] = []
    for raw in FINANCIAL_MANAGER_ACTIONS:
        item = dict(raw)
        mode = str(item.get("mode") or "read_only")
        item["execution_mode"] = _financial_execution_mode(item)
        item["available"] = False
        item["status"] = "unavailable"
        item["side_effect"] = {
            "level": "read_only" if mode == "read_only" else "stateful" if mode == "stateful_intent" else "trade_risk",
            "target": item.get("intent_action") or item.get("tool") or item.get("mcp_tool") or item.get("action_id"),
            "confirmation_required": mode != "read_only",
            "idempotent": mode == "read_only",
        }
        if mode == "blocked":
            item["status"] = "blocked"
            item["availability"] = {"reason_code": "blocked", "blocked_reason": item.get("blocked_reason")}
        elif item.get("tool"):
            item["available"] = str(item["tool"]) in names
            item["status"] = "ready" if item["available"] else "missing_tool"
            item["availability"] = _financial_tool_availability_detail(runtime, item)
        elif item.get("mcp_tool"):
            mcp = mcp_lookup.get(str(item["mcp_tool"]))
            internal_mcp = _internal_financial_mcp_lookup().get(str(item["mcp_tool"]))
            item["available"] = bool(mcp or internal_mcp)
            item["status"] = "ready" if item["available"] else "missing_mcp_tool"
            item["availability"] = _financial_mcp_availability_detail(
                runtime,
                item,
                mcp_lookup,
                mcp_tools=mcp_tools,
                mcp_servers=mcp_servers,
            )
            if mcp:
                item["wrapped_tool"] = mcp.get("wrapped_name")
            if internal_mcp and not mcp:
                item["internal_tool"] = internal_mcp.get("name")
                item["internal_route"] = "desktop_internal_mcp_read_only"
        elif item.get("intent_action"):
            item["available"] = "agent_action_intent_create" in names
            item["status"] = "intent_ready" if item["available"] else "missing_intent_tool"
            item["availability"] = {
                "required_tool": "agent_action_intent_create",
                "reason_code": "action_intent_ready" if item["available"] else "action_intent_tool_missing",
                "agent_registry_has_tool": item["available"],
            }
        actions.append(item)
    summary: dict[str, int] = {}
    for item in actions:
        status = str(item.get("status") or "unknown")
        summary[status] = summary.get(status, 0) + 1
    return {
        "object": "aiask.desktop.financial_manager.catalog",
        "groups": list(FINANCIAL_MANAGER_GROUPS),
        "actions": actions,
        "summary": summary,
        "safety": {
            "mode": "read_only_plus_intents",
            "live_trading_enabled": False,
            "stateful_execution": "allowlisted_confirmed_actions",
            "confirmed_action_scope": list(FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE.keys()),
            "dry_run_only_actions": list(FINANCIAL_MANAGER_DRY_RUN_ONLY_ACTIONS),
            "matrix": safety_matrix,
            "secrets_redacted": True,
        },
        "safety_matrix": safety_matrix,
        "stateful_execution": "allowlisted_confirmed_actions",
        "confirmed_action_scope": list(FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE.keys()),
        "dry_run_only_actions": list(FINANCIAL_MANAGER_DRY_RUN_ONLY_ACTIONS),
        "secrets_redacted": True,
    }


async def financial_status_payload_for_runtime(
    runtime: AgentRuntime,
    *,
    intent_store: Any | None = None,
) -> dict[str, Any]:
    catalog = financial_catalog_payload_for_runtime(runtime)
    readiness = await financial_system_readiness(
        runtime,
        full_runtime=None,
        full_mode_enabled=hermes_full_enabled(),
        control_token_configured=control_token_configured(),
        ai_status=ai_status_payload_for_runtime(runtime),
        intent_store=intent_store,
    )
    mcp = MCPAggregator()
    return {
        "object": "aiask.desktop.financial_manager.status",
        "status": readiness.get("status") or "unknown",
        "readiness": _redact_secrets(readiness),
        "catalog_summary": catalog.get("summary") or {},
        "mcp": {
            "registration": _redact_secrets(mcp.registration_diagnostics()),
            "servers": _redact_secrets(mcp.servers_summary(include_all=False)),
        },
        "stateful_execution": "allowlisted_confirmed_actions",
        "confirmed_action_scope": list(FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE.keys()),
        "dry_run_only_actions": list(FINANCIAL_MANAGER_DRY_RUN_ONLY_ACTIONS),
        "safety_matrix": catalog.get("safety_matrix") or {},
        "broker": {
            "live_trading_enabled": False,
            "read_only_surfaces": ["ths_query_position", "ths_query_orders", "ths_query_deals", "qmt_query_account", "qmt_query_position", "qmt_query_orders"],
            "blocked_actions": ["ths_place_order", "ths_cancel_order", "qmt_place_order", "qmt_cancel_order"],
            "token_env_presence": (catalog.get("safety_matrix") or {}).get("broker_token_policy", {}).get("env_presence", {}),
        },
        "recent_intents": [],
        "secrets_redacted": True,
    }


def _manager_arguments(action: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    mcp_action = str(action.get("mcp_action") or "").strip()
    if not mcp_action:
        return dict(params)
    payload = {key: value for key, value in dict(params).items() if key != "action"}
    return {**payload, "action": mcp_action, "params": payload}


async def _call_internal_desktop_mcp(
    runtime: AgentRuntime,
    action: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    raw_tool = str(action.get("mcp_tool") or "").strip()
    mcp_action = str(action.get("mcp_action") or "").strip()
    mcp = MCPAggregator()
    tool = mcp.internal_financial_tool(raw_tool)
    side_effect = mcp_call_side_effect(
        raw_tool,
        {"action": mcp_action, "params": dict(arguments or {})},
        raw_side_effect={"level": "read_only", "target": f"{raw_tool}.{mcp_action or 'read'}"},
    )
    if side_effect.get("level") != "read_only":
        return aiask_envelope(
            False,
            data={"tool": raw_tool, "side_effect": side_effect},
            error="financial manager MCP action is not read-only",
            tool_name=f"desktop_internal_mcp_{raw_tool}",
            source_chain=["aiask_agent.server", "financial_manager.internal_mcp"],
            side_effect=side_effect,
            error_code="MCP_STATEFUL_ACTION_BLOCKED",
        )
    if not tool:
        return aiask_envelope(
            False,
            data=None,
            error=f"MCP financial tool is not available for internal Desktop use: {raw_tool}",
            tool_name=f"desktop_internal_mcp_{raw_tool}",
            source_chain=["aiask_agent.server", "financial_manager.internal_mcp"],
            error_code="FINANCIAL_TOOL_UNAVAILABLE",
        )
    context = request_context_payload(arguments)
    invocation = runtime.session_store.start_tool_invocation(
        tool_name=f"desktop_internal_mcp.{raw_tool}",
        arguments=arguments,
        user_id=context["user_id"],
        session_id=context.get("session_id"),
        run_id=context.get("run_id"),
        trace_id=context.get("trace_id"),
        capability="financial_manager_internal_mcp",
        category="mcp_financial",
        side_effect=side_effect,
        source_chain=["aiask_agent.server", "financial_manager.internal_mcp"],
    )
    try:
        data = await mcp.call_internal_financial_tool(raw_tool, arguments)
        tool_name = f"desktop_internal_mcp_{raw_tool}"
        if isinstance(data, dict) and {"success", "data", "error"}.issubset(data):
            result = ensure_aiask_envelope(
                data,
                tool_name=tool_name,
                source_chain=["aiask_agent.mcp_client", "financial_manager.internal_mcp"],
            )
        else:
            result = aiask_envelope(
                True,
                data=data,
                error=None,
                tool_name=tool_name,
                source_chain=["aiask_agent.mcp_client", "financial_manager.internal_mcp"],
                side_effect=side_effect,
            )
        result["meta"]["tool_contract"] = _redact_secrets(mcp_tool_metadata(tool))
        result = read_only_desktop_result_guard(f"desktop_internal_mcp.{raw_tool}", result)
        runtime.session_store.finish_tool_invocation(
            str(invocation.get("invocation_id") or ""),
            status="succeeded" if result.get("success") else "failed",
            result=result,
            error_code=result.get("error_code"),
            error_summary=result.get("error"),
            duration_ms=0,
        )
        return result
    except Exception as exc:
        result = aiask_envelope(
            False,
            data=None,
            error=str(exc),
            tool_name=f"desktop_internal_mcp_{raw_tool}",
            source_chain=["aiask_agent.mcp_client", "financial_manager.internal_mcp"],
            side_effect=side_effect,
            error_code="MCP_CALL_FAILED",
        )
        runtime.session_store.finish_tool_invocation(
            str(invocation.get("invocation_id") or ""),
            status="failed",
            result=result,
            error_code=result.get("error_code"),
            error_summary=result.get("error"),
            duration_ms=0,
        )
        return result


async def financial_query_payload_for_runtime(
    runtime: AgentRuntime,
    payload: dict[str, Any],
    *,
    tool_caller: ToolCaller | None = None,
) -> dict[str, Any]:
    action = _financial_action_map().get(_financial_action_key(str(payload.get("capability_id") or ""), str(payload.get("action_id") or "")))
    if not action:
        return {"object": "aiask.desktop.financial_manager.query", "success": False, "data": None, "error": "financial manager action is not registered", "error_code": "FINANCIAL_ACTION_NOT_FOUND", "secrets_redacted": True}
    if action.get("mode") == "blocked":
        return {"object": "aiask.desktop.financial_manager.query", "success": False, "data": {"action": action, "reason": action.get("blocked_reason")}, "error": str(action.get("blocked_reason") or "action is blocked"), "error_code": "FINANCIAL_ACTION_BLOCKED", "secrets_redacted": True}
    if action.get("mode") != "read_only":
        return {"object": "aiask.desktop.financial_manager.query", "success": False, "data": {"action": action, "required_endpoint": "/v1/desktop/financial-manager/intent"}, "error": "stateful financial actions must be created as ActionIntent", "error_code": "FINANCIAL_ACTION_REQUIRES_INTENT", "secrets_redacted": True}
    params = dict(action.get("default_params") or {})
    params.update(dict(payload.get("params") or {}))
    tool_name = str(action.get("tool") or "")
    internal_mcp_tool = ""
    if not tool_name and action.get("mcp_tool"):
        mcp_lookup = _wrapped_mcp_lookup(runtime)
        match = mcp_lookup.get(str(action.get("mcp_tool")))
        tool_name = str((match or {}).get("wrapped_name") or "")
        if not tool_name and _internal_financial_mcp_lookup().get(str(action.get("mcp_tool"))):
            internal_mcp_tool = str(action.get("mcp_tool") or "")
        params = _manager_arguments(action, params)
    if not tool_name and not internal_mcp_tool:
        availability = (
            _financial_mcp_availability_detail(runtime, action, _wrapped_mcp_lookup(runtime))
            if action.get("mcp_tool")
            else _financial_tool_availability_detail(runtime, action)
        )
        return {
            "object": "aiask.desktop.financial_manager.query",
            "success": False,
            "data": {"action": action, "availability": _redact_secrets(availability)},
            "error": "financial manager tool is not available",
            "error_code": "FINANCIAL_TOOL_UNAVAILABLE",
            "secrets_redacted": True,
        }
    tool_payload = tool_payload_with_request_context(params, payload)
    if internal_mcp_tool:
        result = await _call_internal_desktop_mcp(runtime, action, tool_payload)
        tool_name = f"desktop_internal_mcp.{internal_mcp_tool}"
    elif tool_caller:
        result = await tool_caller(tool_name, tool_payload)
    else:
        tool = runtime.tool_registry.get(tool_name)
        result = await audited_runtime_tool_call(
            runtime,
            tool_name,
            tool_payload,
            metadata=dict(getattr(tool, "metadata", {}) or {}) if tool else {},
            source_chain=["aiask_agent.server", "financial_manager.query"],
            result_guard=read_only_desktop_result_guard,
        )
    result = read_only_desktop_result_guard(tool_name, result)
    return {
        "object": "aiask.desktop.financial_manager.query",
        "capability_id": action.get("capability_id"),
        "action_id": action.get("action_id"),
        "tool": tool_name,
        "success": bool(result.get("success")),
        "data": _redact_secrets(result.get("data")),
        "error": _redact_secrets(result.get("error")),
        "error_code": result.get("error_code"),
        "meta": _redact_secrets(result.get("meta")),
        "secrets_redacted": True,
    }


def _broker_request_context(payload: dict[str, Any] | None, *, headers: Any | None = None) -> BrokerSyncContext:
    context = request_context_payload(payload, headers=headers)
    return BrokerSyncContext(
        user_id=str(context.get("user_id") or "local"),
        session_id=context.get("session_id"),
        run_id=context.get("run_id"),
        trace_id=context.get("trace_id"),
        source=str(context.get("source") or "desktop"),
    )


def broker_readiness_payload_for_runtime(runtime: AgentRuntime) -> dict[str, Any]:
    return broker_readiness(runtime.session_store)


def broker_accounts_payload_for_runtime(runtime: AgentRuntime, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = dict(payload or {})
    return latest_broker_payload(
        runtime.session_store,
        user_id=str(body.get("user_id") or "").strip() or None,
        provider=str(body.get("provider") or "").strip() or None,
    )


async def broker_sync_payload_for_runtime(
    runtime: AgentRuntime,
    payload: dict[str, Any],
    *,
    headers: Any | None = None,
    tool_caller: ToolCaller | None = None,
) -> dict[str, Any]:
    provider = normalize_provider(payload.get("provider"))
    context = _broker_request_context(payload, headers=headers)

    async def call_financial_query(_path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await financial_query_payload_for_runtime(runtime, body, tool_caller=tool_caller)

    result = await sync_broker_readonly(
        runtime.session_store,
        provider=provider,
        context=context,
        tool_caller=call_financial_query,
        consent=_truthy(payload.get("consent") or payload.get("consent_granted")),
    )
    runtime.session_store.record_activity_event(
        {
            "user_id": context.user_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "trace_id": context.trace_id,
            "page_key": "broker-readonly",
            "route": "/v1/desktop/broker/sync",
            "event_type": "broker_readonly_sync",
            "target_type": "broker",
            "target_id": provider,
            "payload": {
                "success": result.get("success"),
                "error_code": result.get("error_code"),
                "read_only": True,
            },
            "source": context.source,
        }
    )
    return result


def broker_analytics_payload_for_runtime(runtime: AgentRuntime, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = dict(payload or {})
    user_id = str(body.get("user_id") or "").strip() or None
    provider = str(body.get("provider") or "").strip() or None
    broker_profile_id = str(body.get("broker_profile_id") or "").strip() or None
    analytics = run_behavior_analytics(
        runtime.session_store,
        broker_profile_id=broker_profile_id,
        user_id=user_id,
        provider=provider,
        period_start=str(body.get("period_start") or "").strip() or None,
        period_end=str(body.get("period_end") or "").strip() or None,
    )
    return {
        "object": "aiask.desktop.broker_readonly.analytics",
        "success": True,
        "data": {"analytics": analytics},
        "error": None,
        "read_only": True,
        "live_trading_enabled": False,
        "secrets_redacted": True,
        "source_chain": ["aiask_agent.broker_readonly"],
    }


async def financial_intent_payload_for_runtime(
    runtime: AgentRuntime,
    payload: dict[str, Any],
    *,
    tool_caller: ToolCaller | None = None,
) -> dict[str, Any]:
    action = _financial_action_map().get(_financial_action_key(str(payload.get("capability_id") or ""), str(payload.get("action_id") or "")))
    if not action:
        return {"object": "aiask.desktop.financial_manager.intent", "success": False, "data": None, "error": "financial manager action is not registered", "error_code": "FINANCIAL_ACTION_NOT_FOUND", "secrets_redacted": True}
    if action.get("mode") == "blocked":
        return {"object": "aiask.desktop.financial_manager.intent", "success": False, "data": {"action": action, "reason": action.get("blocked_reason")}, "error": str(action.get("blocked_reason") or "action is blocked"), "error_code": "FINANCIAL_ACTION_BLOCKED", "secrets_redacted": True}
    if action.get("mode") != "stateful_intent":
        return {"object": "aiask.desktop.financial_manager.intent", "success": False, "data": {"action": action}, "error": "read-only financial actions do not create intents", "error_code": "FINANCIAL_ACTION_READ_ONLY", "secrets_redacted": True}
    params = dict(action.get("default_params") or {})
    params.update(dict(payload.get("params") or {}))
    intent_action = str(action.get("intent_action") or "")
    try:
        params = normalize_financial_confirmed_action_params(intent_action, params)
    except ValueError as exc:
        error_code = "FINANCIAL_ACTION_OUTSIDE_CONFIRMED_SCOPE"
        error = str(exc)
        if str(exc) == FINANCIAL_MANAGER_DRY_RUN_REQUIRED_ERROR:
            error_code = "FINANCIAL_MANAGER_DRY_RUN_REQUIRED"
            error = "financial manager confirmed action requires dry_run=true"
        return {
            "object": "aiask.desktop.financial_manager.intent",
            "success": False,
            "data": {
                "action": action,
                "params": _redact_secrets(params),
                "required_endpoint": "/v1/desktop/financial-manager/intent",
            },
            "error": error,
            "error_code": error_code,
            "secrets_redacted": True,
        }
    intent_payload = tool_payload_with_request_context(
        {
            "action": intent_action,
            "params": params,
            "rationale": payload.get("rationale") or f"Financial Manager V1 intent for {action.get('label') or action.get('action_id')}",
            "user_id": payload.get("user_id"),
        },
        payload,
    )
    if tool_caller:
        result = await tool_caller("agent_action_intent_create", intent_payload)
    else:
        tool = runtime.tool_registry.get("agent_action_intent_create")
        result = await audited_runtime_tool_call(
            runtime,
            "agent_action_intent_create",
            intent_payload,
            metadata=dict(getattr(tool, "metadata", {}) or {}) if tool else {},
            source_chain=["aiask_agent.server", "financial_manager.intent"],
        )
    return {
        "object": "aiask.desktop.financial_manager.intent",
        "capability_id": action.get("capability_id"),
        "action_id": action.get("action_id"),
        "success": bool(result.get("success")),
        "data": _redact_secrets(result.get("data")),
        "error": _redact_secrets(result.get("error")),
        "error_code": result.get("error_code"),
        "meta": _redact_secrets(result.get("meta")),
        "secrets_redacted": True,
    }
