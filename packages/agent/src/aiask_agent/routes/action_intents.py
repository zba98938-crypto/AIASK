from __future__ import annotations

import time
from typing import Any

from ..intents import ActionIntentStore, public_intent
from ..request_context import request_context_payload, tool_payload_with_request_context
from ..session_store import AgentSessionStore
from ..session_store_utils import redact_audit_text, sanitize_public_response


def _public_execution_params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(params or {}).items()
        if not str(key).startswith("_")
    }


def create_external_action_intent_payload(
    *,
    intent_store: ActionIntentStore,
    session_store: AgentSessionStore,
    action: str,
    params: dict[str, Any],
    request_payload: dict[str, Any],
    headers: Any | None,
    rationale: str,
    source_chain: list[str],
    object_name: str,
) -> dict[str, Any]:
    execution_params = dict(params or {})
    if action.startswith(("memory.", "learning.", "rl.", "job.", "gateway.", "webhook.")):
        execution_params.setdefault("_state_path", str(session_store.path))
    intent_payload = tool_payload_with_request_context(
        {
            "action": action,
            "params": _public_execution_params(params),
            "rationale": rationale,
            "user_id": request_payload.get("user_id"),
        },
        request_payload,
    )
    context = request_context_payload(intent_payload, headers=headers)
    side_effect = {
        "level": "stateful",
        "target": action,
        "confirmation_required": True,
        "idempotent": False,
    }
    started = time.perf_counter()
    invocation = session_store.start_tool_invocation(
        tool_name="agent_action_intent_create",
        arguments=intent_payload,
        user_id=context.get("user_id"),
        session_id=context.get("session_id"),
        run_id=context.get("run_id"),
        trace_id=context.get("trace_id"),
        capability="action_intent_create",
        category="financial_stateful",
        side_effect=side_effect,
        source_chain=source_chain,
    )
    try:
        intent = intent_store.create(
            action=action,
            params=execution_params,
            user_id=context.get("user_id"),
            rationale=rationale,
            ttl_seconds=int(request_payload.get("ttl_seconds") or 86400),
        )
        intent_id = str(intent.get("intent_id") or "")
        result = {
            "object": object_name,
            "success": True,
            "data": {
                "intent": public_intent(intent),
                "required_confirmation_endpoint": f"/intents/{intent_id}/confirm",
            },
            "error": None,
            "error_code": None,
            "meta": {
                "side_effect": side_effect,
                "action_intent_required": True,
                "source_chain": source_chain,
            },
            "secrets_redacted": True,
        }
    except Exception as exc:
        result = {
            "object": object_name,
            "success": False,
            "data": {"action": action},
            "error": redact_audit_text(exc),
            "error_code": "ACTION_INTENT_CREATE_FAILED",
            "meta": {
                "side_effect": side_effect,
                "action_intent_required": True,
                "source_chain": source_chain,
            },
            "secrets_redacted": True,
        }
    result = sanitize_public_response(result, max_depth=10, max_items=300, max_text=4000)
    session_store.finish_tool_invocation(
        str(invocation.get("invocation_id") or ""),
        status="succeeded" if result.get("success") else "failed",
        result=result,
        error_code=result.get("error_code"),
        error_summary=result.get("error"),
        duration_ms=int((time.perf_counter() - started) * 1000),
        action_intent_id=dict(dict(result.get("data") or {}).get("intent") or {}).get("intent_id"),
    )
    return result
