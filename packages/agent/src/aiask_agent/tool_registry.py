from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .adapters import akshare as akshare_adapter
from .adapters import factory_control as factory_control_adapter
from .adapters import quant as quant_adapter
from .adapters import stock_radar as stock_radar_adapter
from .adapters import strategy_factory as strategy_factory_adapter
from .code_graph import build_code_graph_query_handler
from .control_context import control_context_active
from .general_tools import build_general_tool_handlers
from .intents import ActionIntentStore, public_intent
from .mcp_client import MCPAggregator, MCPOAuthRequired
from .memory import FinancialMemoryStore
from .native_capabilities import build_native_capability_handlers
from .plugin_runtime import NativePluginManager
from .quant_research import QuantResearchStore
from .scheduler import AgentJobStore
from .session_store import AgentSessionStore
from .session_store_utils import redact_audit_text, sanitize_public_response
from .tools.catalog import GENERAL_TOOL_CATALOG, FINANCE_SAFE_TOOL_CATALOG, SAFE_TOOL_CATALOG, tool_descriptions
from .tools.contracts import enrich_tool_contract
from .tools.policy import ToolPolicyEngine, assert_safe_catalog_names, build_policy_from_env, ensure_agent_tool_name
from .tools.schemas import TOOL_SCHEMAS
from .tool_risk import mcp_call_side_effect, mcp_tool_is_live_broker_trade_risk, mcp_tool_metadata, strategy_action_params


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


def _sanitize_tool_public_payload(value: Any) -> Any:
    return sanitize_public_response(value, max_depth=12, max_items=500, max_text=4000)


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    metadata: dict[str, Any] | None = None

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class AgentToolRegistry:
    def __init__(self, *, policy_engine: ToolPolicyEngine | None = None) -> None:
        self._tools: dict[str, AgentTool] = {}
        self.policy_engine = policy_engine or ToolPolicyEngine()
        self.catalog: list[dict[str, Any]] = []
        self.intent_store: ActionIntentStore | None = None

    def register(
        self,
        name: str,
        *,
        description: str,
        parameters: dict[str, Any] | None = None,
        handler: ToolHandler,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_name = ensure_agent_tool_name(name)
        contract_metadata = enrich_tool_contract({"name": normalized_name, "description": description, **dict(metadata or {})})
        self.policy_engine.require_allowed(normalized_name, contract_metadata)
        self._tools[normalized_name] = AgentTool(
            name=normalized_name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}, "additionalProperties": True},
            handler=handler,
            metadata=contract_metadata,
        )
        catalog_item = {
            "name": normalized_name,
            "description": description,
            **contract_metadata,
        }
        self.catalog = [item for item in self.catalog if item.get("name") != normalized_name]
        self.catalog.append(catalog_item)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [self._tools[name].openai_schema() for name in self.names()]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        tool = self.get(name)
        if tool is None:
            return aiask_envelope(
                False,
                data=None,
                error=f"tool is not registered: {name}",
                tool_name=name,
                source_chain=["aiask_agent.tool_registry"],
                error_code="TOOL_NOT_FOUND",
            )
        try:
            result = tool.handler(dict(arguments or {}))
            if inspect.isawaitable(result):
                result = await result
            payload = ensure_aiask_envelope(
                result,
                tool_name=name,
                source_chain=["aiask_agent.tool_registry"],
            )
            payload["meta"]["toolset"] = self.policy_engine.toolset
            return payload
        except Exception as exc:
            return aiask_envelope(
                False,
                data=None,
                error=str(exc),
                tool_name=name,
                source_chain=["aiask_agent.tool_registry"],
                error_code="TOOL_EXECUTION_ERROR",
            )


def aiask_envelope(
    success: bool,
    *,
    data: Any,
    error: str | None,
    tool_name: str,
    source_chain: list[str] | None = None,
    side_effect: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    meta = {
        "trace_id": f"aiask-agent:{tool_name}:{int(time.time() * 1000)}:{uuid4().hex[:8]}",
        "source_chain": list(source_chain or ["aiask_agent"]),
        "side_effect": side_effect
        or {
            "level": "read_only",
            "target": tool_name,
            "confirmation_required": False,
            "idempotent": True,
        },
    }
    payload = {
        "success": bool(success),
        "data": _sanitize_tool_public_payload(data),
        "error": redact_audit_text(error) if error is not None else None,
        "meta": _sanitize_tool_public_payload(meta),
    }
    if error_code:
        payload["error_code"] = error_code
    return payload


def ensure_aiask_envelope(
    value: Any,
    *,
    tool_name: str,
    source_chain: list[str] | None = None,
) -> dict[str, Any]:
    if isinstance(value, dict) and {"success", "data", "error"}.issubset(value):
        result = dict(value)
        meta = dict(result.get("meta") or {})
        meta.setdefault(
            "trace_id",
            f"aiask-agent:{tool_name}:{int(time.time() * 1000)}:{uuid4().hex[:8]}",
        )
        chain = meta.get("source_chain") or source_chain or ["aiask_agent"]
        meta["source_chain"] = [str(item) for item in chain if str(item).strip()]
        side_effect = meta.get("side_effect")
        if not isinstance(side_effect, dict):
            side_effect = {
                "level": "read_only",
                "target": tool_name,
                "confirmation_required": False,
                "idempotent": True,
            }
        meta["side_effect"] = side_effect
        meta.setdefault("toolset", "finance_safe")
        result["data"] = _sanitize_tool_public_payload(result.get("data"))
        result["meta"] = _sanitize_tool_public_payload(meta)
        if result.get("error") is not None:
            result["error"] = redact_audit_text(result.get("error"))
        return result
    return aiask_envelope(
        True,
        data=value,
        error=None,
        tool_name=tool_name,
        source_chain=source_chain,
    )


def _plugin_side_effect_descriptor(
    side_effect: Any,
    *,
    tool_name: str,
    plugin: str,
    kind: str,
) -> dict[str, Any]:
    if isinstance(side_effect, dict):
        payload = dict(side_effect)
        level = str(payload.get("level") or payload.get("side_effect") or "stateful").strip() or "stateful"
    else:
        level = str(side_effect or "stateful").strip() or "stateful"
        payload = {}
    payload["level"] = level
    payload.setdefault("target", f"{plugin}.{kind}.{tool_name}")
    if level == "read_only" and not bool(payload.get("confirmation_required")):
        payload.setdefault("confirmation_required", False)
        payload.setdefault("idempotent", True)
    else:
        payload["confirmation_required"] = True
        payload.setdefault("idempotent", False)
    return payload


def build_default_tool_registry(
    intent_store: ActionIntentStore | None = None,
    *,
    policy_engine: ToolPolicyEngine | None = None,
    session_store: AgentSessionStore | None = None,
    memory_store: FinancialMemoryStore | None = None,
    job_store: AgentJobStore | None = None,
    mcp_aggregator: MCPAggregator | None = None,
) -> AgentToolRegistry:
    assert_safe_catalog_names(item["name"] for item in FINANCE_SAFE_TOOL_CATALOG)
    policy_engine = policy_engine or ToolPolicyEngine(build_policy_from_env())
    registry = AgentToolRegistry(policy_engine=policy_engine)
    intents = intent_store or ActionIntentStore()
    registry.intent_store = intents
    sessions = session_store or AgentSessionStore()
    memories = memory_store or FinancialMemoryStore(getattr(sessions, "path", None))
    jobs = job_store or AgentJobStore(getattr(sessions, "path", None))
    mcp = mcp_aggregator or MCPAggregator()
    quant_store = QuantResearchStore(getattr(sessions, "path", None))

    async def tool_catalog(_: dict[str, Any]) -> dict[str, Any]:
        catalog_snapshot = [dict(item) for item in registry.catalog]
        return aiask_envelope(
            True,
            data={
                "toolset": policy_engine.toolset,
                "tools": catalog_snapshot,
                "count": len(catalog_snapshot),
            },
            error=None,
            tool_name="agent_tool_catalog",
            source_chain=["aiask_agent.tools.safe_catalog"],
        )

    async def action_intent_create(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            action = str(arguments.get("action") or "")
            if action.startswith("file."):
                return aiask_envelope(
                    False,
                    data={
                        "action": action,
                        "reason": "file mutation intents must be created by the matching agent_file_* tool",
                    },
                    error="file mutation intents cannot be created through the generic intent tool",
                    tool_name="agent_action_intent_create",
                    source_chain=["aiask_agent.intents", "aiask_agent.filesystem_guard"],
                    side_effect={
                        "level": "filesystem_write",
                        "target": action or "file",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    error_code="FILE_INTENT_DIRECT_CREATE_BLOCKED",
                )
            if action.startswith("browser."):
                return aiask_envelope(
                    False,
                    data={
                        "action": action,
                        "reason": "browser automation intents must be created by the matching agent_browser_* tool",
                    },
                    error="browser automation intents cannot be created through the generic intent tool",
                    tool_name="agent_action_intent_create",
                    source_chain=["aiask_agent.intents", "aiask_agent.browser_guard"],
                    side_effect={
                        "level": "browser_state",
                        "target": action or "browser",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    error_code="BROWSER_INTENT_DIRECT_CREATE_BLOCKED",
                )
            if action.startswith("terminal."):
                return aiask_envelope(
                    False,
                    data={
                        "action": action,
                        "reason": "terminal execution intents must be created by the matching agent_terminal tool",
                    },
                    error="terminal execution intents cannot be created through the generic intent tool",
                    tool_name="agent_action_intent_create",
                    source_chain=["aiask_agent.intents", "aiask_agent.terminal_guard"],
                    side_effect={
                        "level": "process_execution",
                        "target": action or "terminal",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    error_code="TERMINAL_INTENT_DIRECT_CREATE_BLOCKED",
                )
            if action.startswith("process."):
                return aiask_envelope(
                    False,
                    data={
                        "action": action,
                        "reason": "process control intents must be created by the matching agent_process tool",
                    },
                    error="process control intents cannot be created through the generic intent tool",
                    tool_name="agent_action_intent_create",
                    source_chain=["aiask_agent.intents", "aiask_agent.process_guard"],
                    side_effect={
                        "level": "process_control",
                        "target": action or "process",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    error_code="PROCESS_INTENT_DIRECT_CREATE_BLOCKED",
                )
            if action.startswith("code."):
                return aiask_envelope(
                    False,
                    data={
                        "action": action,
                        "reason": "code execution intents must be created by the matching agent_execute_python tool",
                    },
                    error="code execution intents cannot be created through the generic intent tool",
                    tool_name="agent_action_intent_create",
                    source_chain=["aiask_agent.intents", "aiask_agent.code_execution_guard"],
                    side_effect={
                        "level": "code_execution",
                        "target": action or "code",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    error_code="CODE_INTENT_DIRECT_CREATE_BLOCKED",
                )
            if action in {"plugin.tool_call", "plugin.command_call"}:
                return aiask_envelope(
                    False,
                    data={
                        "action": action,
                        "reason": "dynamic plugin execution intents must be created by the matching agent_plugin_* tool",
                    },
                    error="dynamic plugin execution intents cannot be created through the generic intent tool",
                    tool_name="agent_action_intent_create",
                    source_chain=["aiask_agent.intents", "aiask_agent.plugin_guard"],
                    side_effect={
                        "level": "stateful",
                        "target": action or "plugin",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    error_code="PLUGIN_INTENT_DIRECT_CREATE_BLOCKED",
                )
            params = dict(arguments.get("params") or {})
            if action.startswith(("memory.", "learning.", "rl.", "job.", "gateway.", "webhook.")):
                params.setdefault("_state_path", str(sessions.path))
            intent = intents.create(
                action=action,
                params=params,
                user_id=arguments.get("user_id"),
                rationale=arguments.get("rationale"),
                ttl_seconds=int(arguments.get("ttl_seconds") or 86400),
            )
            return aiask_envelope(
                True,
                data={"intent": public_intent(intent)},
                error=None,
                tool_name="agent_action_intent_create",
                source_chain=["aiask_agent.intents"],
                side_effect={
                    "level": "stateful",
                    "target": intent.get("intent_id"),
                    "confirmation_required": True,
                    "idempotent": False,
                },
            )
        except Exception as exc:
            return aiask_envelope(
                False,
                data=None,
                error=redact_audit_text(str(exc)),
                tool_name="agent_action_intent_create",
                source_chain=["aiask_agent.intents"],
                error_code="INTENT_CREATE_FAILED",
            )

    async def action_intent_get(arguments: dict[str, Any]) -> dict[str, Any]:
        intent_id = str(arguments.get("intent_id") or "").strip()
        intent = intents.get(intent_id) if intent_id else None
        if intent is None:
            return aiask_envelope(
                False,
                data=None,
                error=redact_audit_text(f"intent not found: {intent_id}"),
                tool_name="agent_action_intent_get",
                source_chain=["aiask_agent.intents"],
                error_code="NOT_FOUND",
            )
        return aiask_envelope(
            True,
            data={"intent": public_intent(intent)},
            error=None,
            tool_name="agent_action_intent_get",
            source_chain=["aiask_agent.intents"],
        )

    def _job_action_intent_required(
        *,
        tool_name: str,
        action: str,
        arguments: dict[str, Any],
        level: str = "stateful",
        target: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        intent = intents.create(
            action=f"job.{action}",
            params={**dict(arguments or {}), "_state_path": str(sessions.path)},
            user_id=arguments.get("user_id"),
            rationale=arguments.get("rationale") or f"{tool_name} {action} requires ActionIntent confirmation.",
            ttl_seconds=int(arguments.get("ttl_seconds") or 86400),
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
            error=error or "background job mutation requires ActionIntent confirmation",
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

    def _control_context_required(
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

    async def memory_save(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not control_context_active():
                intent = intents.create(
                    action="memory.save",
                    params={**dict(arguments or {}), "_state_path": str(sessions.path)},
                    user_id=arguments.get("user_id"),
                    rationale=arguments.get("rationale") or "Long-term memory writes require ActionIntent confirmation.",
                    ttl_seconds=int(arguments.get("ttl_seconds") or 86400),
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
                    error="memory mutation requires ActionIntent confirmation",
                    tool_name="agent_memory_save",
                    source_chain=["aiask_agent.memory", "aiask_agent.intents"],
                    side_effect={
                        "level": "stateful",
                        "target": arguments.get("symbol") or arguments.get("research_topic") or "memory",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    error_code="ACTION_INTENT_REQUIRED",
                )
            item = memories.add(
                content=str(arguments.get("content") or ""),
                user_id=arguments.get("user_id"),
                symbol=arguments.get("symbol"),
                strategy_id=arguments.get("strategy_id"),
                research_topic=arguments.get("research_topic"),
            )
            return aiask_envelope(
                True,
                data={"memory": item},
                error=None,
                tool_name="agent_memory_save",
                source_chain=["aiask_agent.memory"],
                side_effect={
                    "level": "stateful",
                    "target": item["memory_id"],
                    "confirmation_required": False,
                    "idempotent": False,
                },
            )
        except Exception as exc:
            return aiask_envelope(
                False,
                data=None,
                error=str(exc),
                tool_name="agent_memory_save",
                source_chain=["aiask_agent.memory"],
                error_code="MEMORY_SAVE_FAILED",
            )

    async def memory_search(arguments: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={
                "items": memories.search(
                    query=arguments.get("query"),
                    user_id=arguments.get("user_id"),
                    symbol=arguments.get("symbol"),
                    strategy_id=arguments.get("strategy_id"),
                    research_topic=arguments.get("research_topic"),
                    limit=int(arguments.get("limit") or 20),
                )
            },
            error=None,
            tool_name="agent_memory_search",
            source_chain=["aiask_agent.memory"],
        )

    async def memory(arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "search").strip().lower()
        if action == "save":
            return await memory_save(arguments)
        if action == "search":
            return await memory_search(arguments)
        if action == "status":
            return aiask_envelope(
                True,
                data={"provider": "aiask_builtin", "configured": True},
                error=None,
                tool_name="agent_memory",
                source_chain=["aiask_agent.memory"],
            )
        return aiask_envelope(
            False,
            data=None,
            error=f"unsupported memory action: {action}",
            tool_name="agent_memory",
            source_chain=["aiask_agent.memory"],
            error_code="INVALID_REQUEST",
        )

    async def session_search(arguments: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={
                "items": sessions.search(
                    query=str(arguments.get("query") or ""),
                    session_id=arguments.get("session_id"),
                    user_id=arguments.get("user_id"),
                    limit=int(arguments.get("limit") or 20),
                )
            },
            error=None,
            tool_name="agent_session_search",
            source_chain=["aiask_agent.session_store"],
        )

    async def quant_research_run(arguments: dict[str, Any]) -> dict[str, Any]:
        return await quant_adapter.quant_research_run(arguments, store=quant_store)

    async def job_create(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not control_context_active():
                return _job_action_intent_required(
                    tool_name="agent_job_create",
                    action="create",
                    arguments=arguments,
                    target=str(arguments.get("name") or "job"),
                )
            job = jobs.create(
                name=str(arguments.get("name") or ""),
                prompt=str(arguments.get("prompt") or ""),
                schedule=arguments.get("schedule"),
                interval_seconds=arguments.get("interval_seconds"),
                toolset=str(arguments.get("toolset") or "finance_safe"),
                enabled=bool(arguments.get("enabled", True)),
            )
            return aiask_envelope(
                True,
                data={"job": job},
                error=None,
                tool_name="agent_job_create",
                source_chain=["aiask_agent.scheduler"],
                side_effect={
                    "level": "stateful",
                    "target": job["job_id"],
                    "confirmation_required": False,
                    "idempotent": False,
                },
            )
        except Exception as exc:
            return aiask_envelope(
                False,
                data=None,
                error=str(exc),
                tool_name="agent_job_create",
                source_chain=["aiask_agent.scheduler"],
                error_code="JOB_CREATE_FAILED",
            )

    async def job_list(_: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={"jobs": jobs.list()},
            error=None,
            tool_name="agent_job_list",
            source_chain=["aiask_agent.scheduler"],
        )

    async def job_run(arguments: dict[str, Any]) -> dict[str, Any]:
        if not control_context_active():
            return _control_context_required(
                tool_name="agent_job_run",
                action="job.run",
                level="subrun",
                target=str(arguments.get("job_id") or "job"),
            )
        return aiask_envelope(
            False,
            data=None,
            error="job execution requires an attached AgentRuntime",
            tool_name="agent_job_run",
            source_chain=["aiask_agent.scheduler"],
            error_code="RUNTIME_NOT_ATTACHED",
        )

    async def cronjob(arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            if action == "list":
                data = {"jobs": jobs.list()}
            elif action == "trigger":
                return await job_run(arguments)
            elif action in {"create", "pause", "resume", "update", "remove"} and not control_context_active():
                return _job_action_intent_required(
                    tool_name="agent_cronjob",
                    action=action,
                    arguments=arguments,
                    target=str(arguments.get("job_id") or arguments.get("name") or "cronjob"),
                )
            elif action == "create":
                data = {
                    "job": jobs.create(
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
                data = {"job": jobs.update(str(arguments.get("job_id") or ""), enabled=action == "resume")}
            elif action == "update":
                data = {"job": jobs.update(str(arguments.get("job_id") or ""), **{k: v for k, v in arguments.items() if k in {"name", "prompt", "schedule", "interval_seconds", "toolset", "enabled"}})}
            elif action == "remove":
                data = {"deleted": jobs.delete(str(arguments.get("job_id") or ""))}
            else:
                raise ValueError(f"unsupported cronjob action: {action}")
            return aiask_envelope(
                True,
                data=data,
                error=None,
                tool_name="agent_cronjob",
                source_chain=["aiask_agent.scheduler"],
                side_effect={
                    "level": "stateful" if action != "list" else "read_only",
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

    descriptions = tool_descriptions()
    metadata_by_name = {item["name"]: dict(item) for item in (*FINANCE_SAFE_TOOL_CATALOG, *GENERAL_TOOL_CATALOG)}
    for item in FINANCE_SAFE_TOOL_CATALOG:
        metadata_by_name[item["name"]] = dict(item)
    finance_registrations: tuple[tuple[str, ToolHandler], ...] = (
        ("agent_tool_catalog", tool_catalog),
        ("agent_analyze_stock", akshare_adapter.analyze_stock),
        ("agent_stock_live_quote", akshare_adapter.stock_live_quote),
        ("agent_stock_news_digest", akshare_adapter.stock_news_digest),
        ("agent_governance_check", akshare_adapter.governance_check),
        ("agent_data_validation", akshare_adapter.data_validation),
        ("agent_quant_data_gate", quant_adapter.quant_data_gate),
        ("agent_factor_validation", quant_adapter.factor_validation),
        ("agent_backtest_suite", quant_adapter.backtest_suite),
        ("agent_portfolio_risk", quant_adapter.portfolio_risk),
        ("agent_quant_research_run", quant_research_run),
        ("agent_market_temperature_snapshot", akshare_adapter.market_temperature_snapshot),
        ("agent_market_temperature_cache_readiness", akshare_adapter.market_temperature_cache_readiness),
        ("agent_market_temperature_cache_history", akshare_adapter.market_temperature_cache_history),
        ("agent_market_temperature_industry_history", akshare_adapter.market_temperature_industry_history),
        ("agent_market_temperature_industry_constituents", akshare_adapter.market_temperature_industry_constituents),
        ("agent_market_temperature_forward_validation", akshare_adapter.market_temperature_forward_validation),
        ("agent_factory_status", strategy_factory_adapter.factory_status),
        ("agent_factory_runs", strategy_factory_adapter.factory_runs),
        ("agent_factory_readiness", factory_control_adapter.readiness),
        ("agent_strategy_review_snapshot", strategy_factory_adapter.strategy_review_snapshot),
        ("agent_strategy_domain_events", strategy_factory_adapter.strategy_domain_events),
        ("agent_factory_event_list", strategy_factory_adapter.factory_event_list),
        ("agent_factory_event_preview_tasks", strategy_factory_adapter.factory_event_preview_tasks),
        ("agent_factory_event_lineage", strategy_factory_adapter.factory_event_lineage),
        ("agent_factory_theme_exposure_status", strategy_factory_adapter.factory_theme_exposure_status),
        ("agent_factory_event_outbox_status", strategy_factory_adapter.factory_event_outbox_status),
        ("agent_incubation_factory_status", strategy_factory_adapter.incubation_factory_status),
        ("agent_trade_prediction_status", strategy_factory_adapter.trade_prediction_status),
        ("agent_trade_prediction_outcomes", strategy_factory_adapter.trade_prediction_outcomes),
        ("agent_trade_prediction_matrix", strategy_factory_adapter.trade_prediction_matrix),
        ("agent_stock_radar_status", stock_radar_adapter.status),
        ("agent_stock_radar_candidates", stock_radar_adapter.candidates),
        ("agent_stock_radar_digest", stock_radar_adapter.digest),
        ("agent_action_intent_create", action_intent_create),
        ("agent_action_intent_get", action_intent_get),
        ("agent_memory", memory),
        ("agent_memory_save", memory_save),
        ("agent_memory_search", memory_search),
        ("agent_session_search", session_search),
    )
    for name, handler in finance_registrations:
        if name not in TOOL_SCHEMAS:
            continue
        registry.register(
            name,
            description=descriptions.get(name, name),
            parameters=TOOL_SCHEMAS[name],
            handler=handler,
            metadata=metadata_by_name.get(name, {"category": "financial_read", "side_effect": "read_only"}),
        )
    if policy_engine.policy.general_tools_enabled and policy_engine.toolset == "general_full":
        registry.register(
            "agent_code_graph_query",
            description=descriptions.get("agent_code_graph_query", "Query the AIASK curated code graph."),
            parameters=TOOL_SCHEMAS["agent_code_graph_query"],
            handler=build_code_graph_query_handler(policy_engine.policy),
            metadata=metadata_by_name.get(
                "agent_code_graph_query",
                {"category": "general_read", "side_effect": "read_only"},
            ),
        )
        for name, handler in build_general_tool_handlers(
            policy_engine.policy,
            state_path=getattr(sessions, "path", None),
            intent_store=intents,
        ).items():
            registry.register(
                name,
                description=descriptions.get(name, name),
                parameters=TOOL_SCHEMAS[name],
                handler=handler,
                metadata=metadata_by_name.get(name, {"category": "general_execute", "side_effect": "process"}),
            )
        for name, handler in build_native_capability_handlers(
            policy=policy_engine.policy,
            session_store=sessions,
            intent_store=intents,
            todo_store=None,
        ).items():
            registry.register(
                name,
                description=descriptions.get(name, name),
                parameters=TOOL_SCHEMAS[name],
                handler=handler,
                metadata=metadata_by_name.get(name, {"category": "general_read", "side_effect": "read_only"}),
            )
        for name, handler in {
            "agent_job_create": job_create,
            "agent_job_list": job_list,
            "agent_job_run": job_run,
            "agent_cronjob": cronjob,
        }.items():
            registry.register(
                name,
                description=descriptions.get(name, name),
                parameters=TOOL_SCHEMAS[name],
                handler=handler,
                metadata=metadata_by_name.get(name, {"category": "cron_admin", "side_effect": "stateful"}),
            )
    for mcp_tool in mcp.financial_tools():
        wrapped_name = mcp_tool["wrapped_name"]
        raw_tool_name = str(mcp_tool.get("name") or "")
        if mcp_tool_is_live_broker_trade_risk(mcp_tool):
            continue

        async def mcp_handler(
            arguments: dict[str, Any],
            tool_name: str = wrapped_name,
            raw_name: str = raw_tool_name,
            raw_side_effect: Any = mcp_tool.get("side_effect"),
        ) -> dict[str, Any]:
            side_effect = mcp_call_side_effect(raw_name, arguments, raw_side_effect=raw_side_effect)
            if side_effect.get("level") != "read_only":
                action = str(side_effect.get("target") or raw_name).replace("strategy_manager.", "")
                if raw_name != "strategy_manager":
                    return aiask_envelope(
                        False,
                        data={
                            "tool": raw_name,
                            "side_effect": side_effect,
                            "reason": "Stateful financial MCP tools require an AIASK-native confirmation adapter before direct execution.",
                        },
                        error="Stateful MCP tool calls are blocked by default",
                        tool_name=tool_name,
                        source_chain=["aiask_agent.mcp_client", "aiask_agent.tool_risk"],
                        side_effect=side_effect,
                        error_code="MCP_STATEFUL_ACTION_BLOCKED",
                    )
                return aiask_envelope(
                    False,
                    data={
                        "required_tool": "agent_action_intent_create",
                        "action": f"strategy_manager.{action}",
                        "params": strategy_action_params(arguments),
                        "reason": "Stateful financial MCP actions require a durable confirmation intent.",
                    },
                    error="MCP action requires durable intent confirmation",
                    tool_name=tool_name,
                    source_chain=["aiask_agent.mcp_client", "aiask_agent.tool_risk"],
                    side_effect=side_effect,
                    error_code="ACTION_INTENT_REQUIRED",
                )
            try:
                data = await mcp.call(tool_name, arguments)
                if isinstance(data, dict) and {"success", "data", "error"}.issubset(data):
                    return ensure_aiask_envelope(
                        data,
                        tool_name=tool_name,
                        source_chain=["aiask_agent.mcp_client"],
                    )
                return aiask_envelope(
                    True,
                    data=data,
                    error=None,
                    tool_name=tool_name,
                    source_chain=["aiask_agent.mcp_client"],
                )
            except MCPOAuthRequired as exc:
                return aiask_envelope(
                    False,
                    data=exc.payload,
                    error="MCP OAuth authorization is required",
                    tool_name=tool_name,
                    source_chain=["aiask_agent.mcp_client"],
                    error_code="MCP_OAUTH_REQUIRED",
                )
            except Exception as exc:
                return aiask_envelope(
                    False,
                    data=None,
                    error=str(exc),
                    tool_name=tool_name,
                    source_chain=["aiask_agent.mcp_client"],
                    error_code="MCP_CALL_FAILED",
                )

        registry.register(
            wrapped_name,
            description=mcp_tool["description"],
            parameters=mcp_tool["parameters"],
            handler=mcp_handler,
            metadata=mcp_tool_metadata(mcp_tool),
        )
    if policy_engine.policy.general_tools_enabled and policy_engine.toolset == "general_full":
        plugin_manager = NativePluginManager()
        for plugin_tool in plugin_manager.tool_definitions():
            wrapped_name = plugin_tool["name"]
            plugin_name = str(plugin_tool["plugin"])
            plugin_kind = str(plugin_tool.get("kind") or "tool")
            raw_tool = str(plugin_tool.get("tool") or "")
            plugin_side_effect = _plugin_side_effect_descriptor(
                plugin_tool.get("side_effect") or "stateful",
                tool_name=wrapped_name,
                plugin=plugin_name,
                kind=plugin_kind,
            )

            async def plugin_handler(
                arguments: dict[str, Any],
                tool_name: str = wrapped_name,
                plugin: str = plugin_name,
                raw_name: str = raw_tool,
                kind: str = plugin_kind,
                side_effect: dict[str, Any] = plugin_side_effect,
            ) -> dict[str, Any]:
                try:
                    if side_effect.get("level") != "read_only" or bool(side_effect.get("confirmation_required")):
                        action = "plugin.command_call" if kind == "command" else "plugin.tool_call"
                        intent = intents.create(
                            action=action,
                            params={
                                "plugin": plugin,
                                "tool": raw_name,
                                "kind": kind,
                                "wrapped_name": tool_name,
                                "arguments": dict(arguments or {}),
                                "_created_by_agent_plugin_tool": tool_name,
                            },
                            user_id=arguments.get("user_id"),
                            rationale=arguments.get("rationale")
                            or f"{tool_name} requires ActionIntent confirmation before plugin runner execution.",
                            ttl_seconds=int(arguments.get("ttl_seconds") or 86400),
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
                                "plugin": plugin,
                                "tool": raw_name,
                                "kind": kind,
                            },
                            error="plugin runner execution requires ActionIntent confirmation",
                            tool_name=tool_name,
                            source_chain=["aiask_agent.plugin_runtime", "aiask_agent.intents"],
                            side_effect=side_effect,
                            error_code="ACTION_INTENT_REQUIRED",
                        )
                    data = await plugin_manager.call_tool(tool_name, arguments)
                    return aiask_envelope(
                        True,
                        data=data,
                        error=None,
                        tool_name=tool_name,
                        source_chain=["aiask_agent.plugin_runtime"],
                        side_effect=side_effect,
                    )
                except Exception as exc:
                    return aiask_envelope(
                        False,
                        data=None,
                        error=str(exc),
                        tool_name=tool_name,
                        source_chain=["aiask_agent.plugin_runtime"],
                        error_code="PLUGIN_TOOL_FAILED",
                    )

            registry.register(
                wrapped_name,
                description=plugin_tool["description"],
                parameters=plugin_tool["parameters"],
                handler=plugin_handler,
                metadata={
                    "capability": "dynamic_plugin_tools",
                    "category": "plugins",
                    "side_effect": plugin_side_effect,
                    "plugin": plugin_name,
                },
            )
    return registry
