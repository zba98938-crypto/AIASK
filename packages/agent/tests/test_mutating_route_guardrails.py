from __future__ import annotations

import inspect
from pathlib import Path

from fastapi.routing import APIRoute

from aiask_agent.intents import ActionIntentStore, IntentExecutor
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore


MUTATING_METHODS = {"DELETE", "PATCH", "POST"}

MUTATING_ROUTE_GUARDRAILS: dict[tuple[str, str], str] = {
    ("DELETE", "/v1/jobs/{job_id}"): "control_token_required",
    ("DELETE", "/v1/mcp/servers/{name}"): "full_mode_action_intent",
    ("DELETE", "/v1/responses/{response_id}"): "control_token_required",
    ("DELETE", "/v1/skills/{name}"): "full_mode_action_intent",
    ("DELETE", "/v1/webhooks/{webhook_id}"): "full_mode_action_intent",
    ("PATCH", "/v1/ai/config"): "control_token_required",
    ("PATCH", "/v1/users/local-profile"): "control_token_required",
    ("PATCH", "/v1/users/{user_id}/data-policy"): "control_token_required",
    ("PATCH", "/v1/jobs/{job_id}"): "control_token_required",
    ("PATCH", "/v1/mcp/servers/{name}"): "full_mode_action_intent",
    ("PATCH", "/v1/plugins/{name}"): "full_mode_action_intent",
    ("PATCH", "/v1/rl/config"): "full_mode_action_intent",
    ("PATCH", "/v1/skills/{name}"): "full_mode_action_intent",
    ("POST", "/intents"): "control_token_required",
    ("POST", "/intents/{intent_id}/confirm"): "control_token_required",
    ("POST", "/intents/{intent_id}/deny"): "control_token_required",
    ("POST", "/v1/ai/smoke"): "api_diagnostic",
    ("POST", "/v1/approvals/{approval_id}/{decision}"): "control_token_required",
    ("POST", "/v1/chat/completions"): "api_conversation_write",
    ("POST", "/v1/connectors/{connector_type}/{name}/test"): "full_mode_required",
    ("POST", "/v1/finance/broker/analytics/run"): "control_token_required",
    ("POST", "/v1/finance/broker/sync"): "control_token_required",
    ("POST", "/v1/data/sync-plan"): "api_preview_plan",
    ("POST", "/v1/users/events"): "api_audit_or_feedback",
    ("POST", "/v1/users/feedback"): "api_audit_or_feedback",
    ("POST", "/v1/finance/manager/intent"): "control_token_required",
    ("POST", "/v1/finance/manager/query"): "api_finance_read_only_query",
    ("POST", "/v1/finance/quant/research-runs"): "api_finance_audited_run",
    ("POST", "/v1/users/retention/sweep"): "control_token_required",
    ("POST", "/v1/data/stock-data-sources"): "control_token_required",
    ("POST", "/v1/data/stock-data-sources/test"): "control_token_required",
    ("POST", "/v1/users/local-profile"): "control_token_required",
    ("POST", "/v1/users/{user_id}/delete"): "control_token_required",
    ("POST", "/v1/gateway/direct-deliver"): "full_mode_action_intent",
    ("POST", "/v1/gateway/directory/refresh"): "full_mode_action_intent",
    ("POST", "/v1/gateway/messages/{message_id}/retry"): "full_mode_action_intent",
    ("POST", "/v1/gateway/pairing"): "full_mode_action_intent",
    ("POST", "/v1/gateway/platforms/{platform}/start"): "full_mode_action_intent",
    ("POST", "/v1/gateway/platforms/{platform}/stop"): "full_mode_action_intent",
    ("POST", "/v1/gateway/send"): "full_mode_action_intent",
    ("POST", "/v1/gateway/webhooks/{platform}"): "external_inbound_webhook",
    ("POST", "/v1/hermes/admin/tools/{tool_name}"): "full_mode_tool_policy_delegate",
    ("POST", "/v1/hermes/sessions"): "control_token_required",
    ("POST", "/v1/jobs"): "control_token_required",
    ("POST", "/v1/jobs/{job_id}/run"): "control_token_required",
    ("POST", "/v1/learning/apply"): "full_mode_action_intent",
    ("POST", "/v1/mcp/discover"): "full_mode_action_intent",
    ("POST", "/v1/mcp/oauth/callback"): "full_mode_action_intent",
    ("POST", "/v1/mcp/oauth/start"): "full_mode_required",
    ("POST", "/v1/mcp/prompts/get"): "full_mode_required",
    ("POST", "/v1/mcp/register-local"): "full_mode_action_intent",
    ("POST", "/v1/mcp/resources/read"): "full_mode_required",
    ("POST", "/v1/mcp/servers"): "full_mode_action_intent",
    ("POST", "/v1/plugins"): "full_mode_action_intent",
    ("POST", "/v1/plugins/{name}/commands/{command}/test"): "full_mode_plugin_test_delegate",
    ("POST", "/v1/plugins/{name}/tools/{tool}/test"): "full_mode_plugin_test_delegate",
    ("POST", "/v1/responses"): "api_conversation_write",
    ("POST", "/v1/rl/runs"): "full_mode_action_intent",
    ("POST", "/v1/rl/runs/{run_id}/stop"): "full_mode_action_intent",
    ("POST", "/v1/runs/{run_id}/cancel"): "control_token_required",
    ("POST", "/v1/runs/{run_id}/steer"): "control_token_required",
    ("POST", "/v1/runs/{run_id}/stop"): "control_token_required_delegate",
    ("POST", "/v1/sessions/batch/archive"): "control_token_required",
    ("POST", "/v1/sessions/{session_id}/archive"): "control_token_required",
    ("POST", "/v1/sessions/{session_id}/undo"): "control_token_required",
    ("POST", "/v1/skills"): "full_mode_action_intent",
    ("POST", "/v1/tools/{tool_name}"): "api_read_only_tool_allowlist",
    # The removed /v1/desktop/* facade. These verbs reach one catch-all that
    # always answers 404 and never touches state, so no guard is required --
    # but they must still be classified so a real route cannot hide here.
    ("DELETE", "/v1/desktop/{removed_path:path}"): "removed_route_404",
    ("PATCH", "/v1/desktop/{removed_path:path}"): "removed_route_404",
    ("POST", "/v1/desktop/{removed_path:path}"): "removed_route_404",
    ("POST", "/v1/webhooks"): "full_mode_action_intent",
    ("POST", "/v1/webhooks/{webhook_id}/trigger"): "full_mode_action_intent",
}

CATEGORY_SOURCE_MARKERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "api_audit_or_feedback": (("require_api(",), ("record_activity_events", "record_feedback")),
    "api_conversation_write": (("require_api(",), ("selected.run(",)),
    "api_diagnostic": (("require_api(",), ("ai_smoke_payload",)),
    "api_finance_audited_run": (("require_api(",), ("audited_tool_call",)),
    "api_finance_read_only_query": (("require_api(",), ("financial_query_payload",)),
    "api_preview_plan": (("require_api(",), ("data_sync_plan_payload",)),
    # Must return the structured removal payload and nothing else.
    "removed_route_404": (("removed_route_payload(",), ("404",)),
    "api_read_only_tool_allowlist": (
        ("require_api(",),
        ("metadata_allows_read_only_call",),
        ("is_read_only_tool",),
        ("read_only_result_guard",),
    ),
    "control_token_required": (("require_control(", "control_authorized(", "require_control_token("),),
    "control_token_required_delegate": (("return await run_cancel(",),),
    "external_inbound_webhook": (("require_api(",), ("handle_inbound",), ("record_inbound",)),
    "full_mode_action_intent": (("require_full(",), ("create_external_action_intent_payload",)),
    "full_mode_action_intent_delegate": (("_asset_intent(",), ("desktop_asset.",)),
    "full_mode_plugin_test_delegate": (
        ("require_full(",),
        ("_wrapped_plugin_name(",),
        ("build_full_runtime(",),
        ("tool_registry.call_tool",),
    ),
    "full_mode_tool_policy_delegate": (
        ("require_full(",),
        ("audited_tool_call(",),
        ("hermes.admin_tool",),
    ),
    "full_mode_required": (("require_full(", "full_tool_call("),),
}

LOW_PRIVILEGE_API_CLASSES = {
    "api_audit_or_feedback",
    "api_conversation_write",
    "api_diagnostic",
    "api_finance_audited_run",
    "api_finance_read_only_query",
    "api_preview_plan",
    "api_read_only_tool_allowlist",
}

DANGEROUS_PATH_TOKENS = (
    "/approvals",
    "/archive",
    "/broker/analytics/run",
    "/broker/sync",
    "/cancel",
    "/data-policy",
    "/delete",
    "/direct-deliver",
    "/files",
    "/financial-manager/intent",
    "/gateway/directory",
    "/gateway/messages",
    "/gateway/pairing",
    "/gateway/platforms",
    "/intents",
    "/jobs",
    "/learning",
    "/local-profile",
    "/mcp",
    "/plugins",
    "/retention",
    "/rl",
    "/send",
    "/skills",
    "/steer",
    "/stock-data-sources",
    "/stock-pools",
    "/strategies",
    "/undo",
    "/webhooks",
)


def _mutating_route_sources(tmp_path: Path, monkeypatch) -> dict[tuple[str, str], str]:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "0")
    monkeypatch.delenv("AIASK_AGENT_MCP_CONFIG", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)

    runtime = AgentRuntime(
        model_client=MockModelClient(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=1,
    )
    try:
        app = create_app(
            runtime=runtime,
            intent_executor=IntentExecutor(ActionIntentStore(tmp_path / "intents.sqlite3")),
        )
        routes: dict[tuple[str, str], str] = {}
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted((route.methods or set()) & MUTATING_METHODS):
                routes[(method, route.path)] = inspect.getsource(route.endpoint)
        return routes
    finally:
        runtime.close()


def _format_routes(routes: set[tuple[str, str]]) -> str:
    return "\n".join(f"{method} {path}" for method, path in sorted(routes))


def _source_matches_category(source: str, category: str) -> bool:
    marker_groups = CATEGORY_SOURCE_MARKERS[category]
    return all(any(marker in source for marker in group) for group in marker_groups)


def test_every_mutating_route_has_guardrail_classification(tmp_path, monkeypatch) -> None:
    current_routes = set(_mutating_route_sources(tmp_path, monkeypatch))
    classified_routes = set(MUTATING_ROUTE_GUARDRAILS)

    missing = current_routes - classified_routes
    stale = classified_routes - current_routes
    invalid_categories = {
        category for category in MUTATING_ROUTE_GUARDRAILS.values() if category not in CATEGORY_SOURCE_MARKERS
    }

    assert not missing, f"unclassified mutating routes:\n{_format_routes(missing)}"
    assert not stale, f"stale mutating route classifications:\n{_format_routes(stale)}"
    assert not invalid_categories, f"unknown guardrail categories: {sorted(invalid_categories)}"


def test_mutating_route_classifications_match_handler_guardrails(tmp_path, monkeypatch) -> None:
    route_sources = _mutating_route_sources(tmp_path, monkeypatch)
    mismatched = []

    for route, category in sorted(MUTATING_ROUTE_GUARDRAILS.items()):
        source = route_sources[route]
        if not _source_matches_category(source, category):
            mismatched.append(f"{route[0]} {route[1]} -> {category}")

    assert not mismatched, "route classification does not match handler guardrail markers:\n" + "\n".join(mismatched)


def test_admin_full_tool_route_preserves_tool_action_intent_guards(tmp_path, monkeypatch) -> None:
    route_sources = _mutating_route_sources(tmp_path, monkeypatch)
    source = route_sources[("POST", "/v1/hermes/admin/tools/{tool_name}")]

    assert "internal_control_context" not in source


def test_dangerous_mutating_routes_cannot_be_classified_as_plain_api() -> None:
    violations = []
    for (method, path), category in sorted(MUTATING_ROUTE_GUARDRAILS.items()):
        if category not in LOW_PRIVILEGE_API_CLASSES:
            continue
        lower_path = path.lower()
        token = next((item for item in DANGEROUS_PATH_TOKENS if item in lower_path), None)
        if token:
            violations.append(f"{method} {path} -> {category} matched {token}")

    assert not violations, "dangerous route tokens require control/full/explicit inbound classification:\n" + "\n".join(violations)
