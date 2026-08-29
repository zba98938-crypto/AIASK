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


class _RuntimeHandoffMixin:
    def _active_session_handoff_state(self, session_id: str) -> dict[str, Any] | None:
        session = self.session_store.get_session(session_id)
        metadata = dict((session or {}).get("metadata") or {})
        state = dict(metadata.get("handoff_state") or {})
        if str(state.get("status") or "").strip().lower() != "active":
            return None
        return state

    def _handoff_specialist_policy(self, handoff_state: dict[str, Any] | None) -> dict[str, Any] | None:
        state = dict(handoff_state or {})
        target = str(state.get("target") or "").strip()
        if not target:
            return None
        normalized = self._normalize_handoff_target(target)
        base = HANDOFF_SPECIALIST_POLICIES.get(
            normalized,
            {
                "policy_id": "general_specialist",
                "role": f"Specialist target: {target}",
                "requested_toolset": self.tool_registry.policy_engine.toolset,
                "preferred_tools": (),
                "instructions": (
                    "Honor the handoff reason and summary, preserve context continuity, and keep all stateful or "
                    "external actions behind the active AIASK guardrails."
                ),
            },
        )
        requested_toolset = str(base.get("requested_toolset") or self.tool_registry.policy_engine.toolset)
        current_toolset = self.tool_registry.policy_engine.toolset
        effective_toolset = requested_toolset
        if requested_toolset == GENERAL_FULL_TOOLSET and (
            current_toolset != GENERAL_FULL_TOOLSET or not self.tool_registry.policy_engine.policy.general_tools_enabled
        ):
            effective_toolset = current_toolset
        preferred_tools = [
            str(name)
            for name in tuple(base.get("preferred_tools") or ())
            if self.tool_registry.get(str(name)) is not None
        ]
        return {
            "target": target,
            "normalized_target": normalized,
            "policy_id": str(base.get("policy_id") or normalized or "general_specialist"),
            "role": str(base.get("role") or "Specialist"),
            "requested_toolset": requested_toolset,
            "effective_toolset": effective_toolset,
            "preferred_tools": preferred_tools,
            "instructions": str(base.get("instructions") or ""),
            "handoff_id": state.get("handoff_id"),
            "context_snapshot_id": state.get("context_snapshot_id"),
            "reason": state.get("reason"),
            "summary": state.get("summary"),
        }

    def _model_tools_for_handoff_policy(self, policy: dict[str, Any]) -> list[dict[str, Any]]:
        all_tools = self.tool_registry.openai_tools()
        preferred = {str(name) for name in list(policy.get("preferred_tools") or []) if str(name)}
        if not preferred:
            policy["filtered"] = False
            return all_tools
        filtered = [tool for tool in all_tools if self._openai_tool_name(tool) in preferred]
        if not filtered:
            policy["filtered"] = False
            return all_tools
        policy["filtered"] = True
        return filtered

    @staticmethod
    def _handoff_policy_message(policy: dict[str, Any]) -> str:
        preferred = ", ".join(str(name) for name in list(policy.get("preferred_tools") or [])) or "current advertised tools"
        advertised = ", ".join(str(name) for name in list(policy.get("advertised_tools") or [])) or "current advertised tools"
        return (
            "AIASK handoff specialist policy is active for this turn.\n"
            f"target={policy.get('target') or 'unspecified'}\n"
            f"policy_id={policy.get('policy_id') or 'general_specialist'}\n"
            f"role={policy.get('role') or 'Specialist'}\n"
            f"effective_toolset={policy.get('effective_toolset') or 'unknown'}\n"
            f"context_snapshot_id={policy.get('context_snapshot_id') or 'none'}\n"
            f"preferred_tools={preferred}\n"
            f"advertised_tools={advertised}\n"
            f"instructions={policy.get('instructions') or ''}"
        )

    @staticmethod
    def _normalize_handoff_target(target: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", str(target or "").strip().lower()).strip("_")
        return HANDOFF_TARGET_ALIASES.get(normalized, normalized or "general_specialist")
