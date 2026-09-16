from __future__ import annotations

from pathlib import Path
from typing import Any

from .approvals import ApprovalStore
from .evidence import _path_allowed_for_artifact_read
from .gateway import GatewayMessageStore
from .intents import ActionIntentStore
from .mcp_client import MCPAggregator
from .route_auth import control_token_configured as _control_token_configured
from .route_auth import hermes_full_enabled as _hermes_full_enabled
from .runtime import AgentRuntime
from .session_store import AgentSessionStore


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "archived"}
    return bool(value)

def _run_event_kind(event_type: str, payload: dict[str, Any]) -> str:
    name = str(event_type or "").strip().lower()
    if "approval" in name or "intent" in name:
        return "approval"
    if "gateway" in name or str(payload.get("platform") or "").strip():
        return "gateway"
    if "mcp" in name:
        return "mcp"
    if "failed" in name or "error" in name:
        return "error"
    if "tool" in name:
        return "tool"
    return "system"


def _run_event_severity(event_type: str, payload: dict[str, Any]) -> str:
    name = str(event_type or "").strip().lower()
    if "failed" in name or "error" in name:
        return "error"
    if "blocked" in name or "retry" in name or "cancel" in name:
        return "warning"
    if "completed" in name:
        return "success"
    return str(payload.get("severity") or "info")


def _run_event_title(event_type: str, payload: dict[str, Any]) -> str:
    name = str(event_type or "").strip()
    if payload.get("title"):
        return str(payload.get("title"))
    if payload.get("tool"):
        return f"{name}: {payload.get('tool')}"
    if payload.get("instruction"):
        return f"{name}: steer"
    if payload.get("error"):
        return f"{name}: error"
    return name or "run.event"


def _run_event_jump_target(kind: str, severity: str) -> str:
    if kind == "approval":
        return "tools-intents-approvals"
    if kind == "gateway":
        return "gateway"
    if kind == "mcp":
        return "mcp-connectors"
    if severity == "error":
        return "readiness-health"
    if kind == "tool":
        return "tools-intents-approvals"
    return "runs-events"


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _artifact_content_payload(session_store: AgentSessionStore, artifact_id: str, *, max_bytes: int = 262144) -> dict[str, Any]:
    item = session_store.get_artifact(artifact_id)
    if item is None:
        raise FileNotFoundError(f"artifact not found: {artifact_id}")
    path_text = str(item.get("path") or "").strip()
    if not path_text:
        return {
            "object": "artifact.content",
            "artifact_id": artifact_id,
            "content": item.get("preview_text"),
            "encoding": "preview",
            "truncated": False,
            "artifact": item,
        }
    path = Path(path_text).expanduser().resolve()
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    status = str(item.get("status") or "ready").strip().lower()
    if status in {"blocked", "denied"} or metadata.get("read_allowed") is False or not _path_allowed_for_artifact_read(path):
        return {
            "object": "artifact.content",
            "artifact_id": artifact_id,
            "content": item.get("preview_text"),
            "encoding": "blocked",
            "truncated": False,
            "artifact": item,
        }
    if not path.exists() or not path.is_file():
        return {
            "object": "artifact.content",
            "artifact_id": artifact_id,
            "content": item.get("preview_text"),
            "encoding": "missing",
            "truncated": False,
            "artifact": item,
        }
    limited = max(1, min(int(max_bytes or 262144), 1024 * 1024))
    raw = path.read_bytes()
    truncated = len(raw) > limited
    chunk = raw[:limited]
    if b"\x00" in chunk:
        import base64

        content = base64.b64encode(chunk).decode("ascii")
        encoding = "base64"
    else:
        content = chunk.decode("utf-8", errors="replace")
        encoding = "utf-8"
    return {
        "object": "artifact.content",
        "artifact_id": artifact_id,
        "content": content,
        "encoding": encoding,
        "truncated": truncated,
        "size_bytes": len(raw),
        "artifact": item,
    }


def _normalize_run_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("data") or {})
    event_type = str(event.get("event") or "")
    kind = _run_event_kind(event_type, payload)
    severity = _run_event_severity(event_type, payload)
    normalized = dict(event)
    normalized["kind"] = kind
    normalized["title"] = _run_event_title(event_type, payload)
    normalized["severity"] = severity
    normalized["jump_target"] = _run_event_jump_target(kind, severity)
    normalized["data"] = payload
    normalized["event_type"] = event_type
    normalized["status"] = _first_present(normalized.get("status"), payload.get("status"), severity)
    normalized["tool_name"] = _first_present(payload.get("tool_name"), payload.get("tool"), payload.get("name"))
    normalized["error_message"] = _first_present(payload.get("error_message"), payload.get("error"), payload.get("detail"))
    return normalized


def _run_summary(item: dict[str, Any], session_store: AgentSessionStore) -> dict[str, Any]:
    payload = dict(item.get("payload") or {})
    run_id = str(item.get("run_id") or "")
    events = session_store.list_run_events(run_id, limit=1000)
    normalized_events = [_normalize_run_event(event) for event in events]
    last_event = normalized_events[-1] if normalized_events else None
    tool_count = sum(1 for event in normalized_events if event.get("kind") == "tool")
    approval_count = sum(1 for event in normalized_events if event.get("kind") == "approval")
    error_count = sum(1 for event in normalized_events if str(event.get("severity") or "") == "error")
    return {
        "run_id": run_id,
        "session_id": item.get("session_id"),
        "status": item.get("status"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "event_count": len(normalized_events),
        "tool_call_count": int(payload.get("tool_call_count") or tool_count),
        "approval_count": approval_count,
        "error_count": error_count,
        "response_id": payload.get("response_id"),
        "last_event": last_event,
        "has_errors": error_count > 0,
        "has_pending_approval": any(
            event.get("kind") == "approval" and str(event.get("status") or "").lower() in {"pending", "awaiting_confirmation", "warning", "info"}
            for event in normalized_events
        ),
    }


def _session_summary(
    session: dict[str, Any],
    *,
    session_store: AgentSessionStore,
    intent_store: ActionIntentStore | None = None,
    approval_store: ApprovalStore | None = None,
) -> dict[str, Any]:
    session_id = str(session.get("session_id") or "").strip()
    metadata = dict(session.get("metadata") or {})
    archived = _truthy(metadata.get("archived"))
    latest_run = next(iter(session_store.list_runs(session_id=session_id, limit=1)), None) if session_id else None
    latest_run_summary = _run_summary(latest_run, session_store) if latest_run else None
    message_count = session_store.count_session_messages(session_id) if session_id else 0
    last_message_at = session_store.latest_message_at(session_id) if session_id else None
    last_event = latest_run_summary.get("last_event") if latest_run_summary else None
    store = intent_store or ActionIntentStore()
    pending_intents = list(store.list(status="awaiting_confirmation", limit=500))
    unknown_execution_intents = list(store.list(status="execution_unknown", limit=500))
    pending_approvals = list((approval_store or ApprovalStore(session_store.path)).list(status="pending", limit=500))
    session_pending_intents = [
        item
        for item in pending_intents
        if str((item.get("params") or {}).get("session_id") or item.get("session_id") or "") == session_id
    ]
    session_unknown_execution_intents = [
        item
        for item in unknown_execution_intents
        if str((item.get("params") or {}).get("session_id") or item.get("session_id") or "") == session_id
    ]
    session_pending_approvals = [
        item
        for item in pending_approvals
        if str((item.get("arguments") or {}).get("session_id") or item.get("session_id") or "") == session_id
    ]
    has_errors = bool((latest_run_summary or {}).get("has_errors"))
    status = str((latest_run_summary or {}).get("status") or session.get("status") or "idle")
    payload = {
        "session_id": session.get("session_id"),
        "title": session.get("title") or session.get("session_id"),
        "user_id": session.get("user_id"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "last_message_at": last_message_at or session.get("updated_at") or session.get("created_at"),
        "last_run_id": (latest_run_summary or {}).get("run_id"),
        "last_run_summary": latest_run_summary,
        "last_event": last_event,
        "message_count": message_count,
        "has_errors": has_errors,
        "has_pending_approval": bool(session_pending_intents or session_pending_approvals or (latest_run_summary or {}).get("has_pending_approval")),
        "has_unknown_execution": bool(session_unknown_execution_intents),
        "status": "error" if has_errors else status,
        "archived": archived,
        "archived_at": metadata.get("archived_at"),
        "archived_reason": metadata.get("archived_reason"),
        "metadata": metadata,
    }
    handoff_state = metadata.get("handoff_state") if isinstance(metadata.get("handoff_state"), dict) else {}
    handoff_status = str(metadata.get("handoff_status") or handoff_state.get("status") or "").strip()
    handoff_target = str(metadata.get("handoff_target") or handoff_state.get("target") or metadata.get("active_agent") or "").strip()
    handoff_id = str(metadata.get("handoff_id") or handoff_state.get("handoff_id") or metadata.get("last_handoff_id") or "").strip()
    handoff_context_snapshot_id = str(
        metadata.get("handoff_context_snapshot_id") or handoff_state.get("context_snapshot_id") or ""
    ).strip()
    active_agent = str(metadata.get("active_agent") or "").strip()
    active_context_snapshot_id = str(metadata.get("active_context_snapshot_id") or "").strip()
    if any([handoff_state, handoff_status, handoff_target, handoff_id, handoff_context_snapshot_id, active_agent, active_context_snapshot_id]):
        payload.update(
            {
                "handoff_state": handoff_state or None,
                "handoff_status": handoff_status or None,
                "handoff_target": handoff_target or None,
                "handoff_id": handoff_id or None,
                "handoff_context_snapshot_id": handoff_context_snapshot_id or None,
                "active_agent": active_agent or None,
                "active_context_snapshot_id": active_context_snapshot_id or None,
            }
        )
    return payload


def _session_summary_payload(
    runtime: AgentRuntime,
    *,
    intent_store: ActionIntentStore | None = None,
    user_id: str | None = None,
    limit: int = 100,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    approval_store = ApprovalStore(runtime.session_store.path)
    return [
        _session_summary(
            session,
            session_store=runtime.session_store,
            intent_store=intent_store,
            approval_store=approval_store,
        )
        for session in runtime.session_store.list_sessions(user_id=user_id, limit=limit, include_archived=include_archived)
    ]


def _handoff_runtime_record(session_store: AgentSessionStore, handoff: dict[str, Any]) -> dict[str, Any]:
    payload = dict(handoff or {})
    session = session_store.get_session(str(payload.get("session_id") or ""))
    session_metadata = dict((session or {}).get("metadata") or {})
    state = dict(session_metadata.get("handoff_state") or {})
    state_matches = bool(state) and str(state.get("handoff_id") or "") == str(payload.get("handoff_id") or "")
    runtime_status = str(state.get("status") or payload.get("status") or "requested").strip().lower() if state_matches else str(payload.get("status") or "requested").strip().lower()
    context_snapshot_id = (
        state.get("context_snapshot_id")
        or session_metadata.get("active_context_snapshot_id")
        or session_metadata.get("handoff_context_snapshot_id")
        or dict(payload.get("metadata") or {}).get("context_snapshot_id")
    )
    payload.update(
        {
            "runtime_status": runtime_status,
            "session_title": (session or {}).get("title"),
            "handoff_state": state if state_matches else None,
            "active_agent": session_metadata.get("active_agent") or (state.get("target") if state_matches else None),
            "active_context_snapshot_id": session_metadata.get("active_context_snapshot_id"),
            "resume_context_snapshot_id": context_snapshot_id,
            "resume_ready": bool(context_snapshot_id or state_matches),
            "secrets_redacted": True,
        }
    )
    return payload


def _handoff_queue_payload(
    runtime: AgentRuntime,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    include_completed: bool = False,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower()
    max_rows = max(1, min(int(limit or 100), 500))
    records = [
        _handoff_runtime_record(runtime.session_store, item)
        for item in runtime.session_store.list_handoffs(session_id=session_id, limit=max_rows)
    ]
    if user_id:
        records = [item for item in records if str(item.get("user_id") or "") == str(user_id)]
    if normalized_status and normalized_status not in {"all", "*"}:
        records = [item for item in records if str(item.get("runtime_status") or item.get("status") or "").lower() == normalized_status]
    if not include_completed:
        records = [
            item
            for item in records
            if str(item.get("runtime_status") or item.get("status") or "").lower() not in {"completed", "failed", "cancelled", "canceled"}
        ]
    summary: dict[str, int] = {}
    for item in records:
        key = str(item.get("runtime_status") or item.get("status") or "unknown").lower()
        summary[key] = summary.get(key, 0) + 1
    return {
        "object": "aiask.handoff_queue",
        "implementation": "aiask_native",
        "data": records[:max_rows],
        "count": len(records[:max_rows]),
        "summary": {"total": len(records[:max_rows]), **summary},
        "filters": {
            "user_id": user_id,
            "session_id": session_id,
            "status": normalized_status or None,
            "include_completed": bool(include_completed),
            "limit": max_rows,
        },
        "secrets_redacted": True,
    }


def _session_resume_context_payload(
    runtime: AgentRuntime,
    session_id: str,
    *,
    intent_store: ActionIntentStore | None = None,
) -> dict[str, Any]:
    session = runtime.session_store.get_session(session_id)
    if session is None:
        raise FileNotFoundError(f"session not found: {session_id}")
    summary = _session_summary(
        session,
        session_store=runtime.session_store,
        intent_store=intent_store,
        approval_store=ApprovalStore(runtime.session_store.path),
    )
    metadata = dict(session.get("metadata") or {})
    handoff_state = dict(summary.get("handoff_state") or metadata.get("handoff_state") or {})
    handoff_id = str(summary.get("handoff_id") or handoff_state.get("handoff_id") or metadata.get("last_handoff_id") or "").strip()
    handoff = runtime.session_store.get_handoff(handoff_id) if handoff_id else None
    snapshot_id = str(
        summary.get("active_context_snapshot_id")
        or summary.get("handoff_context_snapshot_id")
        or handoff_state.get("context_snapshot_id")
        or ""
    ).strip()
    snapshot = runtime.session_store.get_context_snapshot(snapshot_id) if snapshot_id else None
    target = summary.get("active_agent") or summary.get("handoff_target") or handoff_state.get("target")
    resume_prompt = (
        f"继续会话 {session_id}。"
        f"当前任务接管目标为 {target or 'default_agent'}；"
        f"请基于上下文快照 {snapshot_id or 'none'}、交接原因和最近消息继续推进。"
    )
    return {
        "object": "aiask.session_resume_context",
        "implementation": "aiask_native",
        "session_id": session_id,
        "session": summary,
        "handoff": _handoff_runtime_record(runtime.session_store, handoff) if handoff else None,
        "handoff_state": handoff_state or None,
        "context_snapshot": snapshot,
        "resume_context": {
            "session_id": session_id,
            "handoff_id": handoff_id or None,
            "target": target,
            "status": summary.get("handoff_status") or handoff_state.get("status"),
            "context_snapshot_id": snapshot_id or None,
            "context_summary_id": (snapshot or {}).get("context_summary_id"),
            "risk_flags": (snapshot or {}).get("risk_flags") or [],
            "source_message_ids": (snapshot or {}).get("source_message_ids") or [],
            "source_ids": (snapshot or {}).get("source_ids") or [],
            "artifact_ids": (snapshot or {}).get("artifact_ids") or [],
            "summary": handoff_state.get("summary"),
            "reason": handoff_state.get("reason"),
            "resume_prompt": resume_prompt,
        },
        "secrets_redacted": True,
    }


def _trace_check(check_id: str, label: str, status: str, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "evidence": dict(evidence or {}),
    }


def _run_trace_eval_payload(runtime: AgentRuntime, run_id: str) -> dict[str, Any]:
    run = runtime.session_store.get_run(run_id)
    if run is None:
        raise FileNotFoundError(f"run not found: {run_id}")
    events = runtime.session_store.list_run_events(run_id, limit=5000)
    normalized_events = [_normalize_run_event(event) for event in events]
    event_types = [str(item.get("event_type") or item.get("event") or "") for item in normalized_events]
    tool_invocations = runtime.session_store.list_tool_invocations(run_id=run_id, limit=1000)
    context_snapshots = runtime.session_store.list_context_snapshots(run_id=run_id, limit=20)
    sources = runtime.session_store.list_sources(run_id=run_id, limit=1000)
    artifacts = runtime.session_store.list_artifacts(run_id=run_id, limit=1000)
    handoff_events = [item for item in normalized_events if str(item.get("event_type") or item.get("event") or "").startswith("handoff.")]
    guardrail_events = [item for item in normalized_events if "guardrail" in str(item.get("event_type") or item.get("event") or "")]
    error_events = [
        item
        for item in normalized_events
        if str(item.get("severity") or "").lower() == "error"
        or "failed" in str(item.get("event_type") or item.get("event") or "").lower()
    ]
    failed_invocations = [
        item
        for item in tool_invocations
        if str(item.get("status") or "").lower() in {"failed", "error", "blocked"}
    ]
    snapshot = context_snapshots[0] if context_snapshots else None
    risk_flags = list((snapshot or {}).get("risk_flags") or [])
    checks: list[dict[str, Any]] = []
    model_started = event_types.count("model.started")
    model_completed = event_types.count("model.completed")
    checks.append(
        _trace_check(
            "model_trace",
            "Model call trace",
            "pass" if model_started and model_completed and model_completed >= model_started else "warn",
            f"model.started={model_started}, model.completed={model_completed}",
            {"started": model_started, "completed": model_completed},
        )
    )
    tool_started = event_types.count("tool.started")
    tool_completed = event_types.count("tool.completed")
    tool_failed = event_types.count("tool.failed")
    checks.append(
        _trace_check(
            "tool_trace",
            "Tool invocation trace",
            "fail" if failed_invocations or tool_failed else "pass" if tool_invocations or tool_started == 0 else "warn",
            f"tool_invocations={len(tool_invocations)}, tool.started={tool_started}, tool.completed={tool_completed}, failed={len(failed_invocations) or tool_failed}",
            {"invocations": len(tool_invocations), "started": tool_started, "completed": tool_completed, "failed": len(failed_invocations) or tool_failed},
        )
    )
    checks.append(
        _trace_check(
            "context_snapshot",
            "Context snapshot",
            "warn" if risk_flags else "pass" if snapshot else "fail",
            "context snapshot present" if snapshot else "context snapshot missing",
            {
                "context_snapshot_id": (snapshot or {}).get("snapshot_id"),
                "risk_flags": risk_flags,
                "source_message_count": len((snapshot or {}).get("source_message_ids") or []),
                "source_count": len((snapshot or {}).get("source_ids") or []),
                "artifact_count": len((snapshot or {}).get("artifact_ids") or []),
            },
        )
    )
    checks.append(
        _trace_check(
            "evidence_chain",
            "Evidence chain",
            "pass" if sources or artifacts else "warn",
            f"sources={len(sources)}, artifacts={len(artifacts)}",
            {"sources": len(sources), "artifacts": len(artifacts)},
        )
    )
    checks.append(
        _trace_check(
            "handoff_trace",
            "Handoff trace",
            "pass" if not handoff_events or any(str(item.get("event_type") or item.get("event") or "") in {"handoff.activated", "handoff.resumed", "handoff.policy_applied"} for item in handoff_events) else "warn",
            f"handoff_events={len(handoff_events)}",
            {"events": [item.get("event_type") or item.get("event") for item in handoff_events]},
        )
    )
    checks.append(
        _trace_check(
            "guardrail_trace",
            "Guardrail trace",
            "warn" if guardrail_events else "pass",
            f"guardrail_events={len(guardrail_events)}",
            {"events": [item.get("event_type") or item.get("event") for item in guardrail_events]},
        )
    )
    failed_checks = [item for item in checks if item.get("status") == "fail"]
    warn_checks = [item for item in checks if item.get("status") == "warn"]
    score = max(0, 100 - 30 * len(failed_checks) - 10 * len(warn_checks))
    status = "failed" if failed_checks else "degraded" if warn_checks else "healthy"
    return {
        "object": "aiask.run_trace_eval",
        "implementation": "aiask_native",
        "run_id": run_id,
        "session_id": run.get("session_id"),
        "status": status,
        "score": score,
        "checks": checks,
        "summary": {
            "event_count": len(events),
            "tool_invocation_count": len(tool_invocations),
            "failed_tool_invocation_count": len(failed_invocations),
            "context_snapshot_count": len(context_snapshots),
            "source_count": len(sources),
            "artifact_count": len(artifacts),
            "handoff_event_count": len(handoff_events),
            "guardrail_event_count": len(guardrail_events),
            "error_event_count": len(error_events),
        },
        "latest_context_snapshot": snapshot,
        "risk_flags": risk_flags,
        "secrets_redacted": True,
    }


def _workbench_summary_payload(
    runtime: AgentRuntime,
    *,
    intent_store: ActionIntentStore | None = None,
    user_id: str | None = None,
    session_limit: int = 8,
    run_limit: int = 8,
) -> dict[str, Any]:
    store = intent_store or ActionIntentStore()
    recent_sessions = _session_summary_payload(
        runtime,
        intent_store=store,
        user_id=user_id,
        limit=max(1, min(int(session_limit or 8), 20)),
    )
    runs = runtime.session_store.list_runs(limit=max(1, min(int(run_limit or 8), 50)))
    run_summaries = [_run_summary(item, runtime.session_store) for item in runs]
    pending_intents = len(store.list(status="awaiting_confirmation", limit=500))
    unknown_execution_intents = len(store.list(status="execution_unknown", limit=500))
    pending_approvals = len(ApprovalStore(runtime.session_store.path).list(status="pending", limit=500))
    gateway_messages = GatewayMessageStore(runtime.session_store.path).list(limit=500)
    gateway_failed = sum(1 for item in gateway_messages if str(item.get("status") or "").lower() in {"failed", "error"})
    mcp_servers = MCPAggregator().servers_summary(include_all=True)
    mcp_degraded = sum(1 for item in mcp_servers if item.get("configured") is False or item.get("status") == "failed")
    return {
        "object": "aiask.workbench.summary",
        "recent_sessions": recent_sessions,
        "recent_runs": run_summaries,
        "queues": {
            "pending_intents": pending_intents,
            "unknown_execution_intents": unknown_execution_intents,
            "pending_approvals": pending_approvals,
            "gateway_failed": gateway_failed,
            "mcp_degraded": mcp_degraded,
        },
        "access": {
            "full_mode_active": bool(_hermes_full_enabled()),
            "control_token_configured": _control_token_configured(),
            "sessions_admin_available": bool(_hermes_full_enabled() and _control_token_configured()),
        },
    }


def _desktop_runs_payload(
    runtime: AgentRuntime,
    *,
    session_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    runs = runtime.session_store.list_runs(session_id=session_id, status=status, limit=limit)
    return {
        "object": "list",
        "data": [_run_summary(item, runtime.session_store) for item in runs],
    }
