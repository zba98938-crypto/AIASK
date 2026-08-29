from __future__ import annotations

from .intents import ActionIntentStore, IntentExecutor
from .runtime import AgentRuntime
from .session_store import AgentSessionStore
from .tool_registry import build_default_tool_registry
from .tools.policy import GENERAL_FULL_TOOLSET, ToolPolicy, ToolPolicyEngine


def build_runtime_and_executor() -> tuple[AgentRuntime, IntentExecutor]:
    session_store = AgentSessionStore()
    intent_store = ActionIntentStore()
    registry = build_default_tool_registry(intent_store, session_store=session_store)
    return AgentRuntime(session_store=session_store, tool_registry=registry), IntentExecutor(intent_store)


class FullRuntimeManager:
    def __init__(self, runtime: AgentRuntime, intent_store: ActionIntentStore | None = None) -> None:
        self._runtime = runtime
        self._intent_store = intent_store
        self._full_runtime: AgentRuntime | None = None

    def current(self) -> AgentRuntime | None:
        return self._full_runtime

    def active(self) -> bool:
        return self._full_runtime is not None

    def build(self) -> AgentRuntime:
        if self._full_runtime is not None:
            return self._full_runtime
        policy = ToolPolicy(
            toolset=GENERAL_FULL_TOOLSET,
            general_tools_enabled=True,
            workspace_roots=self._runtime.tool_registry.policy_engine.policy.workspace_roots,
        )
        self._full_runtime = AgentRuntime(
            model_client=self._runtime.model_client,
            session_store=self._runtime.session_store,
            tool_registry=build_default_tool_registry(
                self._intent_store,
                session_store=self._runtime.session_store,
                policy_engine=ToolPolicyEngine(policy),
            ),
            model=self._runtime.model,
            max_iterations=self._runtime.max_iterations,
            model_timeout_seconds=self._runtime.model_timeout_seconds,
            tool_timeout_seconds=self._runtime.tool_timeout_seconds,
            retry_attempts=self._runtime.retry_attempts,
        )
        return self._full_runtime

    def reset(self) -> None:
        self._full_runtime = None

    def close(self) -> None:
        if self._full_runtime is not None:
            self._full_runtime.close()

    async def aclose(self) -> None:
        if self._full_runtime is not None:
            await self._full_runtime.aclose()
