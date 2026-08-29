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


class _RuntimeToolCallingMixin:
    @staticmethod
    def _openai_tool_name(tool: dict[str, Any]) -> str:
        return str(dict(tool.get("function") or {}).get("name") or "")

    @classmethod
    def _openai_tool_names(cls, tools: list[dict[str, Any]]) -> list[str]:
        return [name for name in (cls._openai_tool_name(tool) for tool in list(tools or [])) if name]

    def _build_context(self, session_id: str, *, extra_system_messages: list[dict[str, Any] | None] | None = None) -> list[dict[str, Any]]:
        history = self.session_store.get_messages(session_id)
        extras = [dict(item) for item in list(extra_system_messages or []) if isinstance(item, dict)]
        if history and history[0].get("role") == "system":
            return [*list(history), *extras]
        return [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}, *extras, *history]

    @staticmethod
    def _normalize_message(message: dict[str, Any]) -> dict[str, Any]:
        payload = dict(message or {})
        role = str(payload.get("role") or "user").strip() or "user"
        payload["role"] = role
        payload.setdefault("content", "")
        return payload

    def _explicit_tool_call_from_context(self, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        text = self._latest_user_text(messages)
        if not text:
            return None
        match = re.search(r"tool:\s*(agent_[A-Za-z0-9_]+)", text)
        explicit_tool_syntax = True
        if not match:
            match = re.search(
                r"(?:调用|执行|运行|使用|call|use|run)\s*(?:工具|tool)?\s*[:：]?\s*(agent_[A-Za-z0-9_]+)",
                text,
                flags=re.IGNORECASE,
            )
            explicit_tool_syntax = False
        if not match:
            return None
        tool_name = match.group(1)
        tool = self.tool_registry.get(tool_name)
        if tool is None:
            return None
        metadata = dict(getattr(tool, "metadata", {}) or {})
        if not explicit_tool_syntax and not metadata_is_read_only(metadata, target=tool_name):
            return None
        arguments = self._json_object_after(text, match.end())
        return {
            "id": f"call_{uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": dumps_json(arguments, ensure_ascii=False, sort_keys=True),
            },
        }

    @staticmethod
    def _latest_user_text(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages or []):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    @staticmethod
    def _json_object_after(text: str, start: int) -> dict[str, Any]:
        tail = str(text or "")[start:].lstrip()
        if not tail.startswith("{"):
            return {}
        try:
            parsed, _ = json.JSONDecoder().raw_decode(tail)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        function = dict(call.get("function") or {})
        name = str(function.get("name") or call.get("name") or "").strip()
        raw_arguments = function.get("arguments", call.get("arguments", {}))
        if isinstance(raw_arguments, dict):
            arguments = dict(raw_arguments)
            corrected = False
        else:
            try:
                parsed = json.loads(str(raw_arguments or "{}"))
                arguments = parsed if isinstance(parsed, dict) else {}
                corrected = False
            except Exception:
                arguments = {}
                corrected = True
        return name, arguments, corrected

    @staticmethod
    def _merge_usage(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current or {})
        for key, value in dict(update or {}).items():
            if isinstance(value, (int, float)):
                merged[key] = merged.get(key, 0) + value
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
        return sum(max(1, len(str(message.get("content") or "")) // 4) + 8 for message in messages)

    @staticmethod
    def _message_role_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for message in list(messages or []):
            role = str(message.get("role") or "message").strip() or "message"
            counts[role] = counts.get(role, 0) + 1
        return counts
