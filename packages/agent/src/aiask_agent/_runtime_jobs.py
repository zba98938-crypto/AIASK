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
from .control_context import control_context_active
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
from .tool_guardrails import ToolLoopGuardrails, attach_guardrail_metadata
from .intents import ActionIntentStore, public_intent
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


class _RuntimeJobsMixin:
    def _job_intent_store(self) -> ActionIntentStore:
        return getattr(self.tool_registry, "intent_store", None) or ActionIntentStore()

    def _job_action_intent_required(
        self,
        *,
        tool_name: str,
        action: str,
        arguments: dict[str, Any],
        level: str = "stateful",
        target: str | None = None,
    ) -> dict[str, Any]:
        intent = self._job_intent_store().create(
            action=f"job.{action}",
            params={**dict(arguments or {}), "_state_path": str(self.session_store.path)},
            user_id=arguments.get("user_id"),
            rationale=arguments.get("rationale") or f"{tool_name} {action} requires ActionIntent confirmation.",
            ttl_seconds=bounded_int(arguments.get("ttl_seconds"), default=86400, minimum=60, maximum=604800),
        )
        intent_id = str(intent.get("intent_id") or "")
        return aiask_envelope(
            False,
            data={
                "required_tool": "agent_action_intent_get",
                "required_confirmation_endpoint": f"/intents/{intent_id}/confirm",
                "intent": public_intent(intent),
                "action_intent_required": True,
                "executed": False,
            },
            error="background job mutation requires ActionIntent confirmation",
            tool_name=tool_name,
            source_chain=["aiask_agent.scheduler", "aiask_agent.intents"],
            side_effect={
                "level": level,
                "target": target or arguments.get("job_id") or arguments.get("name") or "job",
                "confirmation_required": True,
                "idempotent": False,
            },
            error_code="ACTION_INTENT_REQUIRED",
        )

    @staticmethod
    def _job_control_context_required(
        *,
        tool_name: str,
        action: str,
        level: str,
        target: str | None = None,
    ) -> dict[str, Any]:
        message = f"{tool_name} {action} requires a local control-token route."
        return aiask_envelope(
            False,
            data={
                "control_context_required": True,
                "action_intent_required": False,
                "executed": False,
                "action": action,
                "reason": message,
            },
            error=message,
            tool_name=tool_name,
            source_chain=["aiask_agent.scheduler"],
            side_effect={
                "level": level,
                "target": target or action,
                "confirmation_required": True,
                "idempotent": False,
            },
            error_code="CONTROL_CONTEXT_REQUIRED",
        )

    async def _delegate_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = str(arguments.get("task") or "").strip()
        if not task:
            return aiask_envelope(
                False,
                data=None,
                error="task is required",
                tool_name="agent_delegate_task",
                source_chain=["aiask_agent.subagents"],
                error_code="INVALID_REQUEST",
            )
        runtime_context = dict(arguments.get("_aiask_runtime_context") or {})
        runtime_user_id = str(runtime_context.get("user_id") or "").strip()
        requested_user_id = str(arguments.get("user_id") or "").strip()
        if runtime_user_id and requested_user_id and requested_user_id != runtime_user_id:
            return aiask_envelope(
                False,
                data={
                    "delegate_user_guard": {
                        "blocked": True,
                        "reason": "runtime_context_user_mismatch",
                    },
                    "requested_user_id": requested_user_id,
                },
                error="delegate task cannot target a different user than the active runtime",
                tool_name="agent_delegate_task",
                source_chain=["aiask_agent.subagents"],
                side_effect={
                    "level": "subrun",
                    "target": "agent_delegate_task",
                    "confirmation_required": False,
                    "idempotent": False,
                },
                error_code="DELEGATE_USER_SCOPE_VIOLATION",
            )
        requested_toolset = str(arguments.get("toolset") or FINANCE_SAFE_TOOLSET).strip()
        if requested_toolset == GENERAL_FULL_TOOLSET and not self.tool_registry.policy_engine.policy.general_tools_enabled:
            requested_toolset = FINANCE_SAFE_TOOLSET
        child_policy = ToolPolicy(
            toolset=requested_toolset if requested_toolset in {FINANCE_SAFE_TOOLSET, GENERAL_FULL_TOOLSET} else FINANCE_SAFE_TOOLSET,
            general_tools_enabled=self.tool_registry.policy_engine.policy.general_tools_enabled,
            workspace_roots=self.tool_registry.policy_engine.policy.workspace_roots,
        )
        child_registry = build_default_tool_registry(
            policy_engine=ToolPolicyEngine(child_policy),
            session_store=self.session_store,
        )
        from .runtime import AgentRuntime

        child = AgentRuntime(
            model_client=self.model_client,
            tool_registry=child_registry,
            session_store=self.session_store,
            model=self.model,
            max_iterations=bounded_int(
                arguments.get("max_iterations"),
                default=min(self.max_iterations, 4),
                minimum=1,
                maximum=self.max_iterations,
            ),
            model_timeout_seconds=self.model_timeout_seconds,
            tool_timeout_seconds=self.tool_timeout_seconds,
            retry_attempts=self.retry_attempts,
        )
        child.scheduler.stop()
        
        system_prompt = arguments.get("system_prompt")
        role = arguments.get("role")
        messages = []
        if system_prompt or role:
            role_desc = role or "Financial Sub-Agent"
            prompt_content = system_prompt or f"You are acting as: {role_desc}. Focus only on the requested task."
            messages.append({"role": "system", "content": prompt_content})
            
        messages.append({"role": "user", "content": task})
        
        child_user_id = requested_user_id or runtime_user_id or None
        result = await child.run(messages, user_id=child_user_id)
        record = {
            "run_id": result.run_id,
            "response_id": result.response_id,
            "toolset": child_policy.toolset,
            "mode": "delegation_subrun",
            "parent_session_id": runtime_context.get("session_id"),
            "parent_run_id": runtime_context.get("run_id"),
            "parent_tool_call_id": runtime_context.get("parent_tool_call_id"),
            "parent_context_snapshot_id": runtime_context.get("context_snapshot_id"),
            "child_context_snapshot_id": result.context_snapshot_id,
        }
        self.subruns.append(record)
        return aiask_envelope(
            True,
            data={"subrun": record, "content": result.content},
            error=None,
            tool_name="agent_delegate_task",
            source_chain=["aiask_agent.subagents"],
            side_effect={
                "level": "subrun",
                "target": result.run_id,
                "confirmation_required": False,
                "idempotent": False,
            },
        )

    async def _run_job_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not control_context_active():
            return self._job_control_context_required(
                tool_name="agent_job_run",
                action="job.run",
                level="subrun",
                target=str(arguments.get("job_id") or "job"),
            )
        result = await self.scheduler.run_job(str(arguments.get("job_id") or ""))
        return aiask_envelope(
            bool(result.get("success")),
            data=result.get("data"),
            error=result.get("error"),
            tool_name="agent_job_run",
            source_chain=["aiask_agent.scheduler"],
            side_effect={
                "level": "subrun",
                "target": arguments.get("job_id"),
                "confirmation_required": False,
                "idempotent": False,
            },
            error_code=result.get("error_code"),
        )

    async def _cronjob_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "list").strip().lower()
        if action == "trigger":
            return await self._run_job_tool(arguments)
        try:
            if action == "list":
                data = {"jobs": self.job_store.list()}
            elif action in {"create", "pause", "resume", "update", "remove"} and not control_context_active():
                return self._job_action_intent_required(
                    tool_name="agent_cronjob",
                    action=action,
                    arguments=arguments,
                    target=str(arguments.get("job_id") or arguments.get("name") or "cronjob"),
                )
            elif action == "create":
                data = {
                    "job": self.job_store.create(
                        name=str(arguments.get("name") or ""),
                        prompt=str(arguments.get("prompt") or ""),
                        schedule=arguments.get("schedule"),
                        interval_seconds=arguments.get("interval_seconds"),
                        toolset=str(arguments.get("toolset") or "finance_safe"),
                        enabled=bool(arguments.get("enabled", True)),
                        payload={
                            "script": arguments.get("script"),
                            "skills": list(arguments.get("skills") or []),
                            "silent_pattern": arguments.get("silent_pattern"),
                        },
                    )
                }
            elif action in {"pause", "resume"}:
                data = {"job": self.job_store.update(str(arguments.get("job_id") or ""), enabled=action == "resume")}
            elif action == "update":
                data = {"job": self.job_store.update(str(arguments.get("job_id") or ""), **{k: v for k, v in arguments.items() if k in {"name", "prompt", "schedule", "interval_seconds", "toolset", "enabled"}})}
            elif action == "remove":
                data = {"deleted": self.job_store.delete(str(arguments.get("job_id") or ""))}
            else:
                raise ValueError(f"unsupported cronjob action: {action}")
            return aiask_envelope(
                True,
                data=data,
                error=None,
                tool_name="agent_cronjob",
                source_chain=["aiask_agent.scheduler"],
                side_effect={
                    "level": "read_only" if action == "list" else "stateful",
                    "target": arguments.get("job_id") or arguments.get("name") or "cronjob",
                    "confirmation_required": False,
                    "idempotent": False,
                },
            )
        except Exception as exc:
            return aiask_envelope(
                False,
                data=None,
                error=str(exc),
                tool_name="agent_cronjob",
                source_chain=["aiask_agent.scheduler"],
                error_code="CRONJOB_FAILED",
            )
