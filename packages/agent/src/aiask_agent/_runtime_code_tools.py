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
from .intents import ActionIntentStore, public_intent
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


class _RuntimeCodeToolsMixin:
    def _register_runtime_bound_tools(self) -> None:
        policy = self.tool_registry.policy_engine.policy
        if policy.toolset != GENERAL_FULL_TOOLSET or not policy.general_tools_enabled:
            return
        descriptions = {
            "agent_delegate_task": "Delegate a bounded task to an in-process AIASK subagent.",
            "agent_execute_python": "Run Python code with AIASK tool RPC access to selected enabled tools.",
            "agent_job_run": "Run one local AIASK Agent background job immediately.",
            "agent_moa": "Run AIASK native mixture-of-agents synthesis through the configured model client.",
        }
        self.tool_registry.register(
            "agent_execute_python",
            description=descriptions["agent_execute_python"],
            parameters=TOOL_SCHEMAS["agent_execute_python"],
            handler=self._execute_python_tool,
            metadata={"category": "general_execute", "side_effect": "code_execution", "capability": "execute_python"},
        )
        self.tool_registry.register(
            "agent_delegate_task",
            description=descriptions["agent_delegate_task"],
            parameters=TOOL_SCHEMAS["agent_delegate_task"],
            handler=self._delegate_task,
            metadata={"category": "delegation", "side_effect": "subrun", "capability": "delegation"},
        )
        self.tool_registry.register(
            "agent_job_run",
            description=descriptions["agent_job_run"],
            parameters=TOOL_SCHEMAS["agent_job_run"],
            handler=self._run_job_tool,
            metadata={"category": "cron_admin", "side_effect": "subrun", "capability": "cron_run"},
        )
        self.tool_registry.register(
            "agent_moa",
            description=descriptions["agent_moa"],
            parameters=TOOL_SCHEMAS["agent_moa"],
            handler=self._moa_tool,
            metadata={"category": "moa", "side_effect": "external_generation", "capability": "mixture_of_agents"},
        )
        self.tool_registry.register(
            "agent_cronjob",
            description="Unified cron action API: create, list, update, pause, resume, remove, or trigger jobs.",
            parameters=TOOL_SCHEMAS["agent_cronjob"],
            handler=self._cronjob_tool,
            metadata={"category": "cron_admin", "side_effect": "stateful", "capability": "cronjob"},
            )

    async def _moa_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_moa"
        try:
            from .moa import run_moa

            data = await run_moa(
                model_client=self.model_client,
                user_prompt=str(arguments.get("user_prompt") or ""),
                default_model=self.model,
                max_reference_tokens=arguments.get("max_reference_tokens"),
            )
            return aiask_envelope(
                bool(data.get("content")),
                data=data,
                error=None if data.get("content") else "MoA did not produce content",
                tool_name=tool,
                source_chain=["aiask_agent.moa"],
                side_effect={
                    "level": "external_generation",
                    "target": tool,
                    "confirmation_required": False,
                    "idempotent": False,
                },
            )
        except Exception as exc:
            return aiask_envelope(
                False,
                data=None,
                error=str(exc),
                tool_name=tool,
                source_chain=["aiask_agent.moa"],
                error_code="MOA_FAILED",
            )

    async def _execute_python_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_execute_python"
        code = str(arguments.get("code") or "")
        if not code.strip():
            return aiask_envelope(
                False,
                data=None,
                error="code is required",
                tool_name=tool,
                source_chain=["aiask_agent.code_execution"],
                error_code="INVALID_REQUEST",
            )
        guard = WorkspaceGuard(self.tool_registry.policy_engine.policy)
        try:
            cwd = guard.resolve(arguments.get("cwd") or ".", must_exist=True)
        except Exception as exc:
            return aiask_envelope(
                False,
                data=None,
                error=str(exc),
                tool_name=tool,
                source_chain=["aiask_agent.code_execution"],
                error_code="INVALID_CWD",
            )
        if not control_context_active():
            params = {
                key: value
                for key, value in dict(arguments or {}).items()
                if not str(key).startswith("_aiask_")
            }
            params["cwd"] = str(cwd)
            params["_state_path"] = str(self.session_store.path)
            params["_workspace_roots"] = [str(root) for root in guard.roots]
            params["_created_by_agent_code_tool"] = tool
            intent_store = self.tool_registry.intent_store or ActionIntentStore()
            intent = intent_store.create(
                action="code.execute_python",
                params=params,
                user_id=arguments.get("user_id"),
                rationale=str(arguments.get("rationale") or "Python code execution requires ActionIntent confirmation."),
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
                error="python code execution requires ActionIntent confirmation",
                tool_name=tool,
                source_chain=["aiask_agent.code_execution", "aiask_agent.action_intent_guard"],
                side_effect={
                    "level": "code_execution",
                    "target": str(cwd),
                    "confirmation_required": True,
                    "idempotent": False,
                },
                error_code="ACTION_INTENT_REQUIRED",
            )
        max_output = bounded_int(arguments.get("max_output_bytes"), default=65536, minimum=1, maximum=1024 * 1024)
        timeout = bounded_float(arguments.get("timeout_seconds"), default=30.0, minimum=1.0, maximum=300.0)
        max_tool_calls = bounded_int(arguments.get("max_tool_calls"), default=20, minimum=0, maximum=50)
        allowed_tools = self._code_rpc_allowed_tools()
        runtime_context = dict(arguments.get("_aiask_runtime_context") or {})
        parent_tool_call_id = str(runtime_context.get("parent_tool_call_id") or "").strip()
        rpc_user_id = runtime_context.get("user_id")
        rpc_session_id = str(runtime_context.get("session_id") or "").strip() or None
        rpc_run_id = str(runtime_context.get("run_id") or "").strip() or None
        rpc_trace_id = str(runtime_context.get("trace_id") or "").strip() or None
        tool_call_log: list[dict[str, Any]] = []
        tool_call_count = 0

        with tempfile.TemporaryDirectory(prefix="aiask_agent_code_") as tmp:
            tmp_path = Path(tmp)
            module_path = tmp_path / "aiask_tools.py"
            script_path = tmp_path / "snippet.py"
            socket_path: Path | None = None
            rpc_host = ""
            rpc_port = 0

            async def handle_rpc(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                nonlocal tool_call_count
                try:
                    while True:
                        line = await reader.readline()
                        if not line:
                            break
                        try:
                            request = json.loads(line.decode("utf-8"))
                            name = str(request.get("tool") or "").strip()
                            args = dict(request.get("args") or {})
                        except Exception as exc:
                            response = {"success": False, "error": f"invalid RPC request: {exc}", "data": None}
                        else:
                            if name not in allowed_tools:
                                response = {
                                    "success": False,
                                    "error": f"tool is not available in agent_execute_python: {name}",
                                    "data": None,
                                    "error_code": "TOOL_NOT_ALLOWED",
                                }
                            elif tool_call_count >= max_tool_calls:
                                response = {
                                    "success": False,
                                    "error": f"tool call limit reached: {max_tool_calls}",
                                    "data": None,
                                    "error_code": "TOOL_CALL_LIMIT",
                                }
                            else:
                                if name == "agent_terminal":
                                    for blocked in ("background", "approval_id"):
                                        args.pop(blocked, None)
                                started = time.perf_counter()
                                rpc_index = tool_call_count + 1
                                invocation_id = f"{parent_tool_call_id}.rpc.{rpc_index}" if parent_tool_call_id else f"rpc_{uuid4().hex}"
                                tool_metadata = dict(getattr(self.tool_registry.get(name), "metadata", {}) or {})
                                self.session_store.start_tool_invocation(
                                    tool_name=name,
                                    arguments=args,
                                    user_id=rpc_user_id,
                                    session_id=rpc_session_id,
                                    run_id=rpc_run_id,
                                    trace_id=rpc_trace_id,
                                    invocation_id=invocation_id,
                                    capability=tool_metadata.get("capability"),
                                    category=tool_metadata.get("category"),
                                    side_effect=tool_metadata.get("side_effect"),
                                    source_chain=["aiask_agent.runtime", "agent_execute_python.rpc", "aiask_agent.tool_registry"],
                                )
                                if rpc_run_id:
                                    self.session_store.append_run_event(
                                        rpc_run_id,
                                        "tool.rpc.started",
                                        {
                                            "tool": name,
                                            "tool_call_id": invocation_id,
                                            "parent_tool_call_id": parent_tool_call_id or None,
                                        },
                                    )
                                response = await self.tool_registry.call_tool(name, args)
                                response_data = response.get("data") if isinstance(response.get("data"), dict) else {}
                                intent_id = (
                                    dict(response_data or {}).get("intent_id")
                                    or dict(dict(response_data or {}).get("intent") or {}).get("intent_id")
                                )
                                self.session_store.finish_tool_invocation(
                                    invocation_id,
                                    status="succeeded" if response.get("success") else "failed",
                                    result=response,
                                    error_code=response.get("error_code"),
                                    error_summary=response.get("error"),
                                    duration_ms=int((time.perf_counter() - started) * 1000),
                                    action_intent_id=intent_id,
                                )
                                if rpc_run_id:
                                    self.session_store.append_run_event(
                                        rpc_run_id,
                                        "tool.rpc.completed",
                                        {
                                            "tool": name,
                                            "tool_call_id": invocation_id,
                                            "parent_tool_call_id": parent_tool_call_id or None,
                                            "success": bool(response.get("success")),
                                            "error_code": response.get("error_code"),
                                        },
                                    )
                                    try:
                                        evidence = extract_tool_evidence(
                                            self.session_store,
                                            user_id=rpc_user_id,
                                            session_id=rpc_session_id or "default",
                                            run_id=rpc_run_id,
                                            trace_id=rpc_trace_id or "",
                                            tool_call_id=invocation_id,
                                            tool_name=name,
                                            arguments=args,
                                            result=response,
                                        )
                                        for source in evidence.get("sources", []):
                                            self.session_store.append_run_event(
                                                rpc_run_id,
                                                "source.linked",
                                                {
                                                    "tool": name,
                                                    "tool_call_id": invocation_id,
                                                    "parent_tool_call_id": parent_tool_call_id or None,
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
                                            self.session_store.append_run_event(
                                                rpc_run_id,
                                                "artifact.created",
                                                {
                                                    "tool": name,
                                                    "tool_call_id": invocation_id,
                                                    "parent_tool_call_id": parent_tool_call_id or None,
                                                    "artifact_id": artifact.get("artifact_id"),
                                                    "kind": artifact.get("kind"),
                                                    "title": artifact.get("title"),
                                                    "path": artifact.get("path"),
                                                    "status": artifact.get("status"),
                                                },
                                            )
                                    except Exception as exc:
                                        self.session_store.append_run_event(
                                            rpc_run_id,
                                            "evidence.extract_failed",
                                            {
                                                "tool": name,
                                                "tool_call_id": invocation_id,
                                                "parent_tool_call_id": parent_tool_call_id or None,
                                                "error": str(exc),
                                            },
                                        )
                                tool_call_count += 1
                                tool_call_log.append(
                                    {
                                        "tool": name,
                                        "invocation_id": invocation_id,
                                        "parent_tool_call_id": parent_tool_call_id or None,
                                        "success": bool(response.get("success")),
                                        "error_code": response.get("error_code"),
                                        "action_intent_id": intent_id,
                                        "latency_ms": int((time.perf_counter() - started) * 1000),
                                    }
                                )
                        writer.write((dumps_json(response, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
                        await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            if hasattr(asyncio, "start_unix_server") and os.name != "nt":
                socket_path = Path("/tmp") / f"aiask_rpc_{uuid4().hex}.sock"
                server = await asyncio.start_unix_server(handle_rpc, path=str(socket_path))
                rpc_socket = str(socket_path)
            else:
                server = await asyncio.start_server(handle_rpc, host="127.0.0.1", port=0)
                sock = next((item for item in (server.sockets or []) if item.family in {socket.AF_INET, socket.AF_INET6}), None)
                if sock is None:
                    raise RuntimeError("AIASK tool RPC server did not bind a TCP socket")
                rpc_host, rpc_port = sock.getsockname()[:2]
                rpc_socket = ""
            module_path.write_text(self._build_aiask_tools_module(allowed_tools), encoding="utf-8")
            script_path.write_text(code, encoding="utf-8")
            try:
                env = _sanitized_env()
                env["AIASK_RPC_SOCKET"] = rpc_socket
                if rpc_host and rpc_port:
                    env["AIASK_RPC_HOST"] = str(rpc_host)
                    env["AIASK_RPC_PORT"] = str(rpc_port)
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")]).strip(os.pathsep)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(script_path),
                    cwd=str(cwd),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                    timed_out = False
                except asyncio.TimeoutError:
                    proc.kill()
                    stdout, stderr = await proc.communicate()
                    timed_out = True
                out_text, out_truncated = _limit_bytes(stdout or b"", max_output)
                err_text, err_truncated = _limit_bytes(stderr or b"", max_output)
            finally:
                server.close()
                await server.wait_closed()
                if socket_path is not None:
                    try:
                        socket_path.unlink()
                    except FileNotFoundError:
                        pass
        return aiask_envelope(
            proc.returncode == 0 and not timed_out,
            data={
                "cwd": str(cwd),
                "returncode": proc.returncode,
                "stdout": out_text,
                "stderr": err_text,
                "timed_out": timed_out,
                "truncated": out_truncated or err_truncated,
                "available_rpc_tools": sorted(allowed_tools),
                "tool_calls": tool_call_log,
            },
            error=None if proc.returncode == 0 and not timed_out else "python execution failed",
            tool_name=tool,
            source_chain=["aiask_agent.code_execution"],
            side_effect={
                "level": "code_execution",
                "target": str(cwd),
                "confirmation_required": False,
                "idempotent": False,
            },
            error_code=None if proc.returncode == 0 and not timed_out else "PYTHON_EXECUTION_FAILED",
        )

    def _code_rpc_allowed_tools(self) -> set[str]:
        candidates = {
            "agent_web_search",
            "agent_web_extract",
            "agent_file_read",
            "agent_file_write",
            "agent_file_search",
            "agent_file_patch",
            "agent_terminal",
        }
        return {name for name in candidates if self.tool_registry.get(name) is not None}

    @staticmethod
    def _build_aiask_tools_module(tool_names: set[str]) -> str:
        header = '''"""Auto-generated AIASK tool RPC stubs."""
import json
import os
import shlex
import socket
import time

_sock = None

def _connect():
    global _sock
    if _sock is None:
        if os.environ.get("AIASK_RPC_SOCKET"):
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _sock.connect(os.environ["AIASK_RPC_SOCKET"])
        else:
            _sock = socket.create_connection((os.environ["AIASK_RPC_HOST"], int(os.environ["AIASK_RPC_PORT"])), timeout=300)
        _sock.settimeout(300)
    return _sock

def _call(tool_name, args=None, **kwargs):
    payload = dict(args or {})
    payload.update(kwargs)
    conn = _connect()
    conn.sendall((json.dumps({"tool": tool_name, "args": payload}) + "\\n").encode("utf-8"))
    buf = b""
    while not buf.endswith(b"\\n"):
        chunk = conn.recv(65536)
        if not chunk:
            raise RuntimeError("AIASK RPC disconnected")
        buf += chunk
    return json.loads(buf.decode("utf-8"))

def call_tool(tool_name, **kwargs):
    return _call(tool_name, kwargs)

def shell_quote(value):
    return shlex.quote(str(value))

def retry(fn, max_attempts=3, delay=1.0):
    last_error = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                time.sleep(delay * (2 ** attempt))
    raise last_error

'''
        stubs = []
        for name in sorted(tool_names):
            stubs.append(
                f"def {name}(**kwargs):\n"
                f"    return _call({name!r}, kwargs)\n"
            )
        return header + "\n".join(stubs)
