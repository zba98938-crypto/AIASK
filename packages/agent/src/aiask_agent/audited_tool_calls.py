from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .evidence import extract_tool_evidence


async def audited_runtime_tool_call(
    selected: Any,
    tool_name: str,
    payload: dict[str, Any],
    *,
    request_context_payload: Callable[..., dict[str, Any]],
    headers: Any | None = None,
    metadata: dict[str, Any] | None = None,
    source_chain: list[str] | None = None,
    result_guard: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    context = request_context_payload(payload, headers=headers)
    tool_metadata = dict(metadata or {})
    started = time.perf_counter()
    invocation = selected.session_store.start_tool_invocation(
        tool_name=tool_name,
        arguments=payload,
        user_id=context["user_id"],
        session_id=context.get("session_id"),
        run_id=context.get("run_id"),
        trace_id=context.get("trace_id"),
        capability=tool_metadata.get("capability"),
        category=tool_metadata.get("category"),
        side_effect=tool_metadata.get("side_effect"),
        source_chain=source_chain or ["aiask_agent.server", "aiask_agent.tool_registry"],
    )
    result = await selected.tool_registry.call_tool(tool_name, dict(payload or {}))
    if result_guard is not None:
        result = result_guard(tool_name, result)
    data = dict(result.get("data") or {}) if isinstance(result.get("data"), dict) else {}
    approval_id = dict(data.get("approval") or {}).get("approval_id")
    intent_id = data.get("intent_id") or dict(data.get("intent") or {}).get("intent_id")
    selected.session_store.finish_tool_invocation(
        str(invocation.get("invocation_id") or ""),
        status="succeeded" if result.get("success") else "failed",
        result=result,
        error_code=result.get("error_code"),
        error_summary=result.get("error"),
        duration_ms=int((time.perf_counter() - started) * 1000),
        approval_id=approval_id,
        action_intent_id=intent_id,
    )
    run_id = context.get("run_id")
    if run_id:
        try:
            evidence = extract_tool_evidence(
                selected.session_store,
                user_id=context.get("user_id"),
                session_id=str(context.get("session_id") or "default"),
                run_id=str(run_id),
                trace_id=str(context.get("trace_id") or ""),
                tool_call_id=str(invocation.get("invocation_id") or ""),
                tool_name=tool_name,
                arguments=payload,
                result=result,
            )
            for source in evidence.get("sources", []):
                selected.session_store.append_run_event(
                    str(run_id),
                    "source.linked",
                    {
                        "tool": tool_name,
                        "tool_call_id": invocation.get("invocation_id"),
                        "source_id": source.get("source_id"),
                        "source_type": source.get("source_type"),
                        "provider": source.get("provider"),
                        "title": source.get("title"),
                        "url": source.get("url"),
                        "published_at": source.get("published_at"),
                        "data_timestamp": source.get("data_timestamp"),
                    },
                )
            for artifact in evidence.get("artifacts", []):
                selected.session_store.append_run_event(
                    str(run_id),
                    "artifact.created",
                    {
                        "tool": tool_name,
                        "tool_call_id": invocation.get("invocation_id"),
                        "artifact_id": artifact.get("artifact_id"),
                        "kind": artifact.get("kind"),
                        "title": artifact.get("title"),
                        "path": artifact.get("path"),
                        "status": artifact.get("status"),
                    },
                )
        except Exception as exc:
            selected.session_store.append_run_event(
                str(run_id),
                "evidence.extract_failed",
                {"tool": tool_name, "tool_call_id": invocation.get("invocation_id"), "error": str(exc)},
            )
    return result
