from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import socket
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .context import ContextManager
from .context_references import build_context_reference_message
from .evidence import extract_tool_evidence
from .env_config import load_project_env
from .general_tools import WorkspaceGuard, _limit_bytes, _sanitized_env
from .json_utils import dumps_json
from .model_client import ModelClient, ModelResponse, build_model_client_from_env
from .numeric import bounded_float, bounded_int
from .planner import TaskPlanner
from .plugin_runtime import NativePluginManager
from .recovery import retry_async
from .scheduler import AgentJobStore, BackgroundScheduler
from .session_store import AgentSessionStore, now_iso
from .session_store_utils import redact_audit_text
from .tool_guardrails import ToolLoopGuardrails, attach_guardrail_metadata
from .tool_registry import AgentToolRegistry, aiask_envelope, build_default_tool_registry
from .tool_risk import metadata_is_read_only
from .todo import FinancialTodoStore
from .tools.policy import FINANCE_SAFE_TOOLSET, GENERAL_FULL_TOOLSET, ToolPolicy, ToolPolicyEngine
from .tools.schemas import TOOL_SCHEMAS
from .memory import FinancialMemoryStore


DEFAULT_SYSTEM_PROMPT = (
    "You are AIASK Agent, a financial research and strategy review runtime. "
    "Use only the tools currently provided by the AIASK Agent runtime and follow the active toolset policy. "
    "Never request live trading or direct manager access."
)

HANDOFF_TARGET_ALIASES = {
    "risk": "risk_specialist",
    "risk_review": "risk_specialist",
    "risk_specialist": "risk_specialist",
    "portfolio_risk": "risk_specialist",
    "research": "research_specialist",
    "market_research": "research_specialist",
    "research_specialist": "research_specialist",
    "ops": "ops_specialist",
    "operations": "ops_specialist",
    "ops_specialist": "ops_specialist",
}

HANDOFF_SPECIALIST_POLICIES: dict[str, dict[str, Any]] = {
    "risk_specialist": {
        "policy_id": "risk_specialist",
        "role": "Risk specialist",
        "requested_toolset": FINANCE_SAFE_TOOLSET,
        "preferred_tools": (
            "agent_portfolio_risk",
            "agent_data_validation",
            "agent_quant_data_gate",
            "agent_factor_validation",
            "agent_market_temperature_cache_readiness",
            "agent_trade_prediction_status",
            "agent_trade_prediction_matrix",
        ),
        "instructions": (
            "Prioritize exposure, downside risk, data freshness, concentration, and guardrail status. "
            "Do not propose or execute live trades; stateful actions must remain ActionIntent-gated."
        ),
    },
    "research_specialist": {
        "policy_id": "research_specialist",
        "role": "Market research specialist",
        "requested_toolset": FINANCE_SAFE_TOOLSET,
        "preferred_tools": (
            "agent_analyze_stock",
            "agent_stock_live_quote",
            "agent_stock_news_digest",
            "agent_market_temperature_snapshot",
            "agent_market_temperature_industry_history",
            "agent_strategy_review_snapshot",
        ),
        "instructions": (
            "Prioritize evidence-backed market context, source quality, timestamp freshness, and uncertainty. "
            "Separate facts, model inference, and user-facing conclusions."
        ),
    },
    "ops_specialist": {
        "policy_id": "ops_specialist",
        "role": "Operations coordination specialist",
        "requested_toolset": GENERAL_FULL_TOOLSET,
        "preferred_tools": (
            "agent_tool_catalog",
            "agent_factory_status",
            "agent_gateway_status",
            "agent_mcp_manage",
            "agent_session_handoff",
            "agent_todo",
        ),
        "instructions": (
            "Prioritize system status, queue ownership, failed handoffs, and recovery steps. "
            "Keep cross-boundary mutations behind control-token and approval guardrails."
        ),
    },
}


@dataclass
class AgentRunResult:
    response_id: str
    session_id: str
    run_id: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "completed"
    context_summary_id: str | None = None
    context_snapshot_id: str | None = None
    planner_steps: list[dict[str, Any]] = field(default_factory=list)
    subruns: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

from ._runtime_handoff import _RuntimeHandoffMixin
from ._runtime_tool_calling import _RuntimeToolCallingMixin
from ._runtime_code_tools import _RuntimeCodeToolsMixin
from ._runtime_jobs import _RuntimeJobsMixin


class AgentRuntime(_RuntimeHandoffMixin, _RuntimeToolCallingMixin, _RuntimeCodeToolsMixin, _RuntimeJobsMixin):
    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        tool_registry: AgentToolRegistry | None = None,
        session_store: AgentSessionStore | None = None,
        context_manager: ContextManager | None = None,
        planner: TaskPlanner | None = None,
        model: str | None = None,
        max_iterations: int | None = None,
        model_timeout_seconds: float | None = None,
        tool_timeout_seconds: float | None = None,
        retry_attempts: int | None = None,
    ) -> None:
        load_project_env()
        self.model_client = model_client or build_model_client_from_env()
        self.session_store = session_store or AgentSessionStore()
        self.session_store.ensure_ready()
        self.tool_registry = tool_registry or build_default_tool_registry(session_store=self.session_store)
        self.context_manager = context_manager or ContextManager(
            model_client=self.model_client,
            memory_store=FinancialMemoryStore(self.session_store.path)
        )
        self.planner = planner or TaskPlanner(todo_store=FinancialTodoStore(self.session_store.path))
        self.model = model or os.getenv("AIASK_AGENT_MODEL", "gpt-4.1-mini")
        self.max_iterations = bounded_int(
            max_iterations if max_iterations is not None else os.getenv("AIASK_AGENT_MAX_ITERATIONS", "8"),
            default=8,
            minimum=1,
        )
        self.model_timeout_seconds = bounded_float(
            model_timeout_seconds if model_timeout_seconds is not None else os.getenv("AIASK_AGENT_MODEL_TIMEOUT", "120"),
            default=120.0,
            minimum=1.0,
            maximum=3600.0,
        )
        self.tool_timeout_seconds = bounded_float(
            tool_timeout_seconds if tool_timeout_seconds is not None else os.getenv("AIASK_AGENT_TOOL_TIMEOUT", "120"),
            default=120.0,
            minimum=1.0,
            maximum=3600.0,
        )
        self.retry_attempts = bounded_int(
            retry_attempts if retry_attempts is not None else os.getenv("AIASK_AGENT_RETRY_ATTEMPTS", "3"),
            default=3,
            minimum=1,
        )
        self.job_store = AgentJobStore(self.session_store.path)
        self.scheduler = BackgroundScheduler(runtime=self, store=self.job_store)
        self.plugin_manager = NativePluginManager()
        self.subruns: list[dict[str, Any]] = []
        self._register_runtime_bound_tools()
        if os.getenv("AIASK_AGENT_ENABLE_CRON", "").strip().lower() in {"1", "true", "yes", "on"}:
            self.scheduler.start()
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.scheduler.stop()
        closer = getattr(self.model_client, "aclose", None) or getattr(self.model_client, "close", None)
        if callable(closer):
            result = closer()
            if inspect.isawaitable(result):
                await result
        store_closer = getattr(self.session_store, "close", None)
        if callable(store_closer):
            store_closer()

    def close(self) -> None:
        asyncio.run(self.aclose())

    def refresh_tool_registry(self) -> None:
        self.tool_registry = build_default_tool_registry(session_store=self.session_store)
        self._register_runtime_bound_tools()

    async def __aenter__(self) -> "AgentRuntime":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        stream: bool = False,
    ) -> AgentRunResult:
        self.subruns = []
        sid = self.session_store.create_session(session_id=session_id, user_id=user_id)
        trace_id = f"trace_{uuid4().hex}"
        run_id = self.session_store.create_run(
            sid,
            {
                "user_id": user_id,
                "trace_id": trace_id,
                "message_count": len(messages or []),
                "started_at": now_iso(),
                "stream": bool(stream),
            },
        )
        audit_events: list[dict[str, Any]] = []
        persisted_events: list[dict[str, Any]] = []

        def emit(event_type: str, payload: dict[str, Any] | None = None) -> None:
            event = self.session_store.append_run_event(run_id, event_type, dict(payload or {}))
            persisted_events.append(event)
            audit_events.append({**dict(event.get("data") or {}), "event": event_type, "run_id": run_id, "created_at": event["created_at"]})

        emit("run.started", {"session_id": sid, "user_id": user_id, "trace_id": trace_id, "stream": bool(stream)})
        try:
            for plugin_event in await self.plugin_manager.on_session_start(session_id=sid, run_id=run_id):
                emit("plugin.hook", plugin_event)
        except Exception as exc:
            emit("plugin.hook_failed", {"hook": "on_session_start", "error": str(exc)})
        for message in list(messages or []):
            normalized = self._normalize_message(message)
            self.session_store.append_message(sid, normalized)

        handoff_state = self.session_store.consume_session_handoff_state(sid, run_id=run_id, trace_id=trace_id)
        handoff_resumed = False
        if not handoff_state:
            handoff_state = self._active_session_handoff_state(sid)
            handoff_resumed = bool(handoff_state)
        handoff_context_message: dict[str, Any] | None = None
        if handoff_state:
            if handoff_resumed:
                emit(
                    "handoff.resumed",
                    {
                        "handoff_id": handoff_state.get("handoff_id"),
                        "target": handoff_state.get("target"),
                        "context_snapshot_id": handoff_state.get("context_snapshot_id"),
                        "source_run_id": handoff_state.get("source_run_id"),
                    },
                )
            else:
                emit(
                    "handoff.activated",
                    {
                        "handoff_id": handoff_state.get("handoff_id"),
                        "target": handoff_state.get("target"),
                        "context_snapshot_id": handoff_state.get("context_snapshot_id"),
                        "source_run_id": handoff_state.get("source_run_id"),
                    },
                )
            handoff_context_message = {
                "role": "system",
                "name": "handoff_state",
                "content": (
                    f"AIASK session handoff is {'resumed' if handoff_resumed else 'active'} for this turn.\n"
                    f"target={handoff_state.get('target') or 'unspecified'}\n"
                    f"handoff_id={handoff_state.get('handoff_id') or 'unknown'}\n"
                    f"context_snapshot_id={handoff_state.get('context_snapshot_id') or 'none'}\n"
                    f"reason={handoff_state.get('reason') or ''}\n"
                    f"summary={handoff_state.get('summary') or ''}"
                ),
                "metadata": {"handoff_state": handoff_state},
            }
        handoff_policy = self._handoff_specialist_policy(handoff_state)
        handoff_policy_message: dict[str, Any] | None = None
        active_model_tools = self.tool_registry.openai_tools()
        if handoff_policy:
            active_model_tools = self._model_tools_for_handoff_policy(handoff_policy)
            advertised_tools = self._openai_tool_names(active_model_tools)
            handoff_policy = {
                **handoff_policy,
                "advertised_tools": advertised_tools,
                "advertised_tool_count": len(advertised_tools),
            }
            emit(
                "handoff.policy_applied",
                {
                    "target": handoff_policy.get("target"),
                    "policy_id": handoff_policy.get("policy_id"),
                    "role": handoff_policy.get("role"),
                    "requested_toolset": handoff_policy.get("requested_toolset"),
                    "effective_toolset": handoff_policy.get("effective_toolset"),
                    "preferred_tools": handoff_policy.get("preferred_tools"),
                    "advertised_tools": advertised_tools,
                    "filtered": bool(handoff_policy.get("filtered")),
                },
            )
            handoff_policy_message = {
                "role": "system",
                "name": "handoff_specialist_policy",
                "content": self._handoff_policy_message(handoff_policy),
                "metadata": {"handoff_policy": handoff_policy},
            }

        context_message, context_reference_records = build_context_reference_message(
            messages=[dict(item) for item in messages if isinstance(item, dict)],
            store=self.session_store,
            session_id=sid,
            run_id=run_id,
            trace_id=trace_id,
            user_id=user_id,
        )
        if context_reference_records:
            emit(
                "context.references_resolved",
                {
                    "count": len(context_reference_records),
                    "sources": [
                        {
                            "source_id": item.get("source_id"),
                            "title": item.get("title"),
                            "url": item.get("url"),
                        }
                        for item in context_reference_records
                        if item.get("source_id")
                    ],
                    "artifacts": [
                        {
                            "artifact_id": item.get("artifact_id"),
                            "kind": item.get("kind"),
                            "title": item.get("title"),
                            "path": item.get("path"),
                        }
                        for item in context_reference_records
                        if item.get("artifact_id")
                    ],
                },
            )
        extra_context_messages = [item for item in (handoff_context_message, handoff_policy_message, context_message) if item]
        working_messages = self._build_context(sid, extra_system_messages=extra_context_messages)
        planner_result = self.planner.plan(messages=working_messages, session_id=sid, user_id=user_id)
        planner_steps = planner_result.steps
        if planner_steps:
            emit("planner.plan_created", {"steps": planner_steps})
            if planner_result.prompt_message:
                insert_at = 1 if working_messages and working_messages[0].get("role") == "system" else 0
                working_messages.insert(insert_at, planner_result.prompt_message)
        snapshot_message_rows = self.session_store.list_session_messages(sid, limit=2000)
        snapshot_source_message_ids = [str(item.get("id")) for item in snapshot_message_rows if item.get("id") is not None]
        estimated_tokens_before_context = self._estimate_tokens(working_messages)
        context_result = await self.context_manager.prepare(working_messages, user_id=user_id)
        context_summary_id = context_result.summary_id
        estimated_tokens_after_context = self._estimate_tokens(context_result.messages)
        if context_result.compacted:
            emit(
                "context.compacted",
                {
                    "context_summary_id": context_summary_id,
                    "estimated_tokens_before": estimated_tokens_before_context,
                    "estimated_tokens_after": estimated_tokens_after_context,
                },
            )
            self.session_store.append_message(
                sid,
                {
                    "role": "system",
                    "name": "context_summary",
                    "content": context_result.summary or "",
                    "metadata": {"context_summary_id": context_summary_id},
                },
            )
            working_messages = context_result.messages
        context_snapshot_id: str | None = None
        try:
            context_source_ids = [
                str(item.get("source_id"))
                for item in context_reference_records
                if item.get("source_id")
            ]
            context_artifact_ids = [
                str(item.get("artifact_id"))
                for item in context_reference_records
                if item.get("artifact_id")
            ]
            risk_flags: list[str] = []
            if context_result.compacted:
                risk_flags.append("lossy_compression")
                if estimated_tokens_after_context >= estimated_tokens_before_context:
                    risk_flags.append("token_estimate_not_reduced")
            if any(item.get("error") for item in context_reference_records):
                risk_flags.append("unresolved_reference")
            snapshot = self.session_store.record_context_snapshot(
                session_id=sid,
                user_id=user_id,
                run_id=run_id,
                trace_id=trace_id,
                context_summary_id=context_summary_id,
                policy="runtime_prepare",
                compacted=context_result.compacted,
                message_count=len(context_result.messages),
                source_message_ids=snapshot_source_message_ids,
                source_ids=context_source_ids,
                artifact_ids=context_artifact_ids,
                token_estimate_before=estimated_tokens_before_context,
                token_estimate_after=estimated_tokens_after_context,
                summary=context_result.summary,
                summary_model=self.model if context_result.compacted else None,
                risk_flags=risk_flags,
                metadata={
                    "context_reference_count": len(context_reference_records),
                    "context_reference_message": bool(context_message),
                    "handoff_policy": handoff_policy,
                    "compaction_policy": {
                        "max_tokens": self.context_manager.max_tokens,
                        "head_messages": self.context_manager.head_messages,
                        "tail_messages": self.context_manager.tail_messages,
                    },
                    "role_counts": self._message_role_counts(context_result.messages),
                },
            )
            context_snapshot_id = str(snapshot.get("snapshot_id") or "") or None
            emit(
                "context.snapshot_created",
                {
                    "context_snapshot_id": context_snapshot_id,
                    "context_summary_id": context_summary_id,
                    "compacted": context_result.compacted,
                    "message_count": len(context_result.messages),
                    "source_count": len(context_source_ids),
                    "artifact_count": len(context_artifact_ids),
                    "risk_flags": risk_flags,
                },
            )
        except Exception as exc:
            emit("context.snapshot_failed", {"error": str(exc)})
        all_tool_calls: list[dict[str, Any]] = []
        total_usage: dict[str, Any] = {}
        final_content = ""
        status = "completed"
        explicit_tool_directive_used = False
        tool_guardrails = ToolLoopGuardrails()
        guardrail_halt: dict[str, Any] | None = None

        try:
            for iteration in range(1, self.max_iterations + 1):
                model_started = time.perf_counter()
                emit("model.started", {"iteration": iteration, "model": self.model})

                async def call_model() -> Any:
                    nonlocal explicit_tool_directive_used
                    if not explicit_tool_directive_used:
                        explicit_tool_call = self._explicit_tool_call_from_context(working_messages)
                        if explicit_tool_call:
                            explicit_tool_directive_used = True
                            emit(
                                "tool.directive_detected",
                                {
                                    "tool": explicit_tool_call["function"]["name"],
                                    "tool_call_id": explicit_tool_call["id"],
                                    "iteration": iteration,
                                },
                            )
                            return ModelResponse(content="", tool_calls=[explicit_tool_call], usage={})
                    model_messages, plugin_events = await self.plugin_manager.pre_llm_call(
                        messages=working_messages,
                        model=self.model,
                        tools=active_model_tools,
                    )
                    for plugin_event in plugin_events:
                        emit("plugin.hook", plugin_event)
                    return await asyncio.wait_for(
                        self.model_client.complete(
                            messages=model_messages,
                            tools=active_model_tools,
                            model=self.model,
                        ),
                        timeout=self.model_timeout_seconds,
                    )

                model_response = await retry_async(
                    call_model,
                    attempts=self.retry_attempts,
                    audit_events=audit_events,
                    event_factory=lambda event: emit("model.retry", event),
                )
                try:
                    for plugin_event in await self.plugin_manager.post_llm_call(
                        messages=working_messages,
                        model=self.model,
                        response={
                            "content": model_response.content,
                            "tool_calls": model_response.tool_calls,
                            "usage": model_response.usage,
                        },
                    ):
                        emit("plugin.hook", plugin_event)
                except Exception as exc:
                    emit("plugin.hook_failed", {"hook": "post_llm_call", "error": str(exc)})
                emit(
                    "model.completed",
                    {
                        "iteration": iteration,
                        "latency_ms": int((time.perf_counter() - model_started) * 1000),
                        "tool_call_count": len(model_response.tool_calls),
                    },
                )
                if model_response.content:
                    emit("model.delta", {"iteration": iteration, "content": model_response.content})
                elif not model_response.tool_calls:
                    emit("model.empty_response", {"iteration": iteration})
                total_usage = self._merge_usage(total_usage, model_response.usage)
                assistant_message = {
                    "role": "assistant",
                    "content": model_response.content,
                }
                if model_response.tool_calls:
                    assistant_message["tool_calls"] = model_response.tool_calls
                working_messages.append(assistant_message)
                self.session_store.append_message(sid, assistant_message)
                final_content = model_response.content or final_content

                if not model_response.tool_calls:
                    break

                for call in model_response.tool_calls:
                    tool_name, arguments, args_corrected = self._parse_tool_call(call)
                    tool_call_record = {
                        "id": call.get("id") or f"call_{uuid4().hex[:12]}",
                        "name": tool_name,
                        "arguments": arguments,
                    }
                    if args_corrected:
                        emit(
                            "tool.arguments_corrected",
                            {
                                "tool": tool_name,
                                "tool_call_id": tool_call_record["id"],
                                "iteration": iteration,
                            },
                        )
                    tool_started = time.perf_counter()
                    blocked, plugin_events = await self.plugin_manager.pre_tool_call(
                        tool_name=tool_name,
                        arguments=arguments,
                        session_id=sid,
                        run_id=run_id,
                    )
                    for plugin_event in plugin_events:
                        emit("plugin.hook", plugin_event)
                    if blocked:
                        result = aiask_envelope(
                            False,
                            data={"block": blocked},
                            error=str(blocked.get("reason") or "blocked by plugin hook"),
                            tool_name=tool_name,
                            source_chain=["aiask_agent.plugin_runtime"],
                            error_code="PLUGIN_HOOK_BLOCKED",
                        )
                        self.session_store.start_tool_invocation(
                            tool_name=tool_name,
                            arguments=arguments,
                            user_id=user_id,
                            session_id=sid,
                            run_id=run_id,
                            trace_id=trace_id,
                            invocation_id=tool_call_record["id"],
                            source_chain=["aiask_agent.runtime", "aiask_agent.plugin_runtime"],
                        )
                        self.session_store.finish_tool_invocation(
                            tool_call_record["id"],
                            status="blocked",
                            result=result,
                            error_code=result.get("error_code"),
                            error_summary=result.get("error"),
                            duration_ms=int((time.perf_counter() - tool_started) * 1000),
                        )
                        tool_call_record["result"] = result
                        all_tool_calls.append(tool_call_record)
                        emit(
                            "tool.blocked",
                            {
                                "tool": tool_name,
                                "tool_call_id": tool_call_record["id"],
                                "plugin": blocked.get("plugin"),
                                "reason": blocked.get("reason"),
                            },
                        )
                        working_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_record["id"],
                                "name": tool_name,
                                "content": dumps_json(result, ensure_ascii=False),
                            }
                        )
                        self.session_store.append_message(sid, working_messages[-1])
                        continue
                    emit(
                        "tool.started",
                        {
                            "tool": tool_name,
                            "tool_call_id": tool_call_record["id"],
                            "iteration": iteration,
                        },
                    )
                    tool_metadata = dict(getattr(self.tool_registry.get(tool_name), "metadata", {}) or {})
                    self.session_store.start_tool_invocation(
                        tool_name=tool_name,
                        arguments=arguments,
                        user_id=user_id,
                        session_id=sid,
                        run_id=run_id,
                        trace_id=trace_id,
                        invocation_id=tool_call_record["id"],
                        capability=tool_metadata.get("capability"),
                        category=tool_metadata.get("category"),
                        side_effect=tool_metadata.get("side_effect"),
                        source_chain=["aiask_agent.runtime", "aiask_agent.tool_registry"],
                    )

                    execution_arguments = arguments
                    if tool_name in {
                        "agent_terminal",
                        "agent_process",
                        "agent_execute_python",
                        "agent_delegate_task",
                        "agent_session_handoff",
                        "agent_todo_set",
                        "agent_todo_list",
                        "agent_todo",
                        "agent_subgoal",
                    }:
                        execution_arguments = {
                            **arguments,
                            "_aiask_runtime_context": {
                                "user_id": user_id,
                                "session_id": sid,
                                "run_id": run_id,
                                "trace_id": trace_id,
                                "parent_tool_call_id": tool_call_record["id"],
                                "context_snapshot_id": context_snapshot_id,
                            },
                        }

                    async def call_tool() -> dict[str, Any]:
                        return await asyncio.wait_for(
                            self.tool_registry.call_tool(tool_name, execution_arguments),
                            timeout=self.tool_timeout_seconds,
                        )

                    result = await retry_async(
                        call_tool,
                        attempts=self.retry_attempts
                        if metadata_is_read_only(
                            dict(getattr(self.tool_registry.get(tool_name), "metadata", {}) or {}),
                            target=tool_name,
                        )
                        else 1,
                        audit_events=audit_events,
                        event_factory=lambda event: emit("tool.retry", event),
                    )
                    result, plugin_events = self.plugin_manager.transform_tool_result(tool_name=tool_name, result=result)
                    for plugin_event in plugin_events:
                        emit("plugin.hook", plugin_event)
                    try:
                        for plugin_event in await self.plugin_manager.post_tool_call(
                            tool_name=tool_name,
                            arguments=arguments,
                            result=result,
                            session_id=sid,
                            run_id=run_id,
                        ):
                            emit("plugin.hook", plugin_event)
                    except Exception as exc:
                        emit("plugin.hook_failed", {"hook": "post_tool_call", "tool": tool_name, "error": str(exc)})
                    if result.get("error_code") == "APPROVAL_REQUIRED":
                        approval_id = dict(dict(result.get("data") or {}).get("approval") or {}).get("approval_id")
                        emit(
                            "approval.pending",
                            {
                                "tool": tool_name,
                                "tool_call_id": tool_call_record["id"],
                                "approval": dict(result.get("data") or {}).get("approval"),
                            },
                        )
                    else:
                        approval_id = None
                    if tool_name == "agent_clarify" and dict(result.get("data") or {}).get("requires_user_input"):
                        emit(
                            "clarify.pending",
                            {
                                "tool": tool_name,
                                "tool_call_id": tool_call_record["id"],
                                "question": dict(result.get("data") or {}).get("question"),
                                "options": dict(result.get("data") or {}).get("options") or [],
                            },
                        )
                    if tool_name == "agent_delegate_task" and result.get("success"):
                        emit("subagent.spawned", dict(result.get("data") or {}))
                    if tool_name in {"agent_cronjob", "agent_job_run"}:
                        emit("cron.triggered", {"tool": tool_name, "success": bool(result.get("success"))})
                    if tool_name == "agent_webhook":
                        emit("webhook.triggered", {"success": bool(result.get("success"))})
                    guardrail_decision = tool_guardrails.observe(tool_name, arguments, result)
                    if guardrail_decision is not None:
                        result = attach_guardrail_metadata(result, guardrail_decision)
                        emit(
                            "tool.guardrail_halt" if guardrail_decision.action == "halt" else "tool.guardrail_warning",
                            {
                                "tool": tool_name,
                                "tool_call_id": tool_call_record["id"],
                                **guardrail_decision.to_metadata(),
                            },
                        )
                    tool_call_record["result"] = result
                    all_tool_calls.append(tool_call_record)
                    result_data = dict(result.get("data") or {}) if isinstance(result.get("data"), dict) else {}
                    intent_id = result_data.get("intent_id") or dict(result_data.get("intent") or {}).get("intent_id")
                    self.session_store.finish_tool_invocation(
                        tool_call_record["id"],
                        status="succeeded" if result.get("success") else "failed",
                        result=result,
                        error_code=result.get("error_code"),
                        error_summary=result.get("error"),
                        duration_ms=int((time.perf_counter() - tool_started) * 1000),
                        approval_id=approval_id,
                        action_intent_id=intent_id,
                    )
                    try:
                        evidence = extract_tool_evidence(
                            self.session_store,
                            user_id=user_id,
                            session_id=sid,
                            run_id=run_id,
                            trace_id=trace_id,
                            tool_call_id=tool_call_record["id"],
                            tool_name=tool_name,
                            arguments=arguments,
                            result=result,
                        )
                        if evidence.get("sources"):
                            tool_call_record["sources"] = [
                                {
                                    "source_id": item.get("source_id"),
                                    "source_type": item.get("source_type"),
                                    "title": item.get("title"),
                                    "url": item.get("url"),
                                    "provider": item.get("provider"),
                                }
                                for item in evidence.get("sources", [])
                            ]
                        if evidence.get("artifacts"):
                            tool_call_record["artifacts"] = [
                                {
                                    "artifact_id": item.get("artifact_id"),
                                    "kind": item.get("kind"),
                                    "title": item.get("title"),
                                    "path": item.get("path"),
                                    "status": item.get("status"),
                                }
                                for item in evidence.get("artifacts", [])
                            ]
                        for source in evidence.get("sources", []):
                            emit(
                                "source.linked",
                                {
                                    "tool": tool_name,
                                    "tool_call_id": tool_call_record["id"],
                                    "source_id": source.get("source_id"),
                                    "source_type": source.get("source_type"),
                                    "provider": source.get("provider"),
                                    "title": source.get("title"),
                                    "url": source.get("url"),
                                    "published_at": source.get("published_at"),
                                    "data_timestamp": source.get("data_timestamp"),
                                },
                            )
                            if source.get("source_type") == "news":
                                emit(
                                    "news.source_linked",
                                    {
                                        "tool": tool_name,
                                        "tool_call_id": tool_call_record["id"],
                                        "source_id": source.get("source_id"),
                                        "title": source.get("title"),
                                        "url": source.get("url"),
                                        "provider": source.get("provider"),
                                        "published_at": source.get("published_at"),
                                    },
                                )
                        for artifact in evidence.get("artifacts", []):
                            emit(
                                "artifact.created",
                                {
                                    "tool": tool_name,
                                    "tool_call_id": tool_call_record["id"],
                                    "artifact_id": artifact.get("artifact_id"),
                                    "kind": artifact.get("kind"),
                                    "title": artifact.get("title"),
                                    "path": artifact.get("path"),
                                    "status": artifact.get("status"),
                                },
                            )
                            if artifact.get("kind") == "quote_snapshot":
                                emit(
                                    "market.quote_snapshot",
                                    {
                                        "tool": tool_name,
                                        "tool_call_id": tool_call_record["id"],
                                        "artifact_id": artifact.get("artifact_id"),
                                        "title": artifact.get("title"),
                                        "preview": artifact.get("preview_json"),
                                    },
                                )
                            if artifact.get("kind") == "terminal_output":
                                emit(
                                    "terminal.output_artifact",
                                    {
                                        "tool": tool_name,
                                        "tool_call_id": tool_call_record["id"],
                                        "artifact_id": artifact.get("artifact_id"),
                                        "title": artifact.get("title"),
                                    },
                                )
                    except Exception as exc:
                        emit(
                            "evidence.extract_failed",
                            {
                                "tool": tool_name,
                                "tool_call_id": tool_call_record["id"],
                                "error": str(exc),
                            },
                        )
                    emit(
                        "tool.completed" if result.get("success") else "tool.failed",
                        {
                            "tool": tool_name,
                            "tool_call_id": tool_call_record["id"],
                            "success": bool(result.get("success")),
                            "latency_ms": int((time.perf_counter() - tool_started) * 1000),
                            "error_code": result.get("error_code"),
                        },
                    )
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call_record["id"],
                        "name": tool_name,
                        "content": dumps_json(result, ensure_ascii=False, sort_keys=True),
                    }
                    working_messages.append(tool_message)
                    self.session_store.append_message(sid, tool_message)
                    if guardrail_decision is not None and guardrail_decision.action == "halt":
                        guardrail_halt = guardrail_decision.to_metadata()
                        status = "guardrail_halted"
                        final_content = guardrail_decision.message
                        break
                if guardrail_halt is not None:
                    break
            else:
                status = "max_iterations_reached"
                final_content = final_content or "Agent stopped after reaching max_iterations."
                emit("run.max_iterations_reached", {"max_iterations": self.max_iterations})
        except Exception as exc:
            status = "failed"
            final_content = f"Agent run failed: {redact_audit_text(exc)}"
            emit("run.failed", {"error": str(exc)})

        if status != "failed" and not final_content:
            if all_tool_calls:
                names = ", ".join(item.get("name", "unknown_tool") for item in all_tool_calls)
                final_content = f"工具调用完成: {names}"
                emit("response.fallback", {"reason": "empty_model_after_tool_calls", "tool_call_count": len(all_tool_calls)})
            else:
                final_content = "AIASK model returned an empty response."
                emit("response.fallback", {"reason": "empty_model_response"})

        if status != "failed":
            emit("run.completed", {"status": status})
        try:
            for plugin_event in await self.plugin_manager.on_session_end(session_id=sid, run_id=run_id, status=status):
                emit("plugin.hook", plugin_event)
        except Exception as exc:
            emit("plugin.hook_failed", {"hook": "on_session_end", "error": str(exc)})
        response_id = f"resp_{uuid4().hex}"
        result = AgentRunResult(
            response_id=response_id,
            session_id=sid,
            run_id=run_id,
            content=final_content,
            tool_calls=all_tool_calls,
            usage=total_usage,
            audit_events=audit_events,
            messages=working_messages,
            events=persisted_events,
            status=status,
            context_summary_id=context_summary_id,
            context_snapshot_id=context_snapshot_id,
            planner_steps=planner_steps,
            subruns=list(self.subruns),
        )
        try:
            from .learning_loop import LearningLoop

            learning_events = LearningLoop(session_store=self.session_store, state_path=self.session_store.path).observe_run(result)
            for learning_event in learning_events:
                emit("learning.observed", learning_event)
            result.audit_events = audit_events
            result.events = persisted_events
        except Exception as exc:
            emit("learning.failed", {"error": str(exc)})
            result.audit_events = audit_events
            result.events = persisted_events
        def final_payload() -> dict[str, Any]:
            payload = result.to_dict()
            payload.setdefault("user_id", user_id)
            payload.setdefault("trace_id", trace_id)
            payload.setdefault("message_count", len(messages or []))
            payload.setdefault("stream", bool(stream))
            return payload

        payload = final_payload()
        try:
            for plugin_event in await self.plugin_manager.on_session_finalize(
                session_id=sid,
                run_id=run_id,
                status=status,
                response=payload,
            ):
                emit("plugin.hook", plugin_event)
            result.audit_events = audit_events
            result.events = persisted_events
            payload = final_payload()
        except Exception as exc:
            emit("plugin.hook_failed", {"hook": "on_session_finalize", "error": str(exc)})
            result.audit_events = audit_events
            result.events = persisted_events
            payload = final_payload()
        self.session_store.save_response(response_id, sid, payload)
        self.session_store.update_run(run_id, status=status, payload=payload)
        return result
