from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable


AGENT_TOOL_PREFIX = "agent_"
FORBIDDEN_DIRECT_MANAGER_TOKENS = (
    "strategy_manager",
    "live_trading_manager",
    "paper_trading_manager",
    "execution_manager",
    "available_tools",
    "get_tool_contract",
)

FINANCE_SAFE_TOOLSET = "finance_safe"
GENERAL_FULL_TOOLSET = "general_full"


@dataclass(frozen=True)
class ToolPolicy:
    toolset: str
    general_tools_enabled: bool
    workspace_roots: tuple[str, ...]

    def allows_category(self, category: str) -> bool:
        normalized = str(category or "financial_read").strip().lower()
        if normalized in {"financial_read", "financial_stateful"}:
            return True
        if normalized == "mcp_financial":
            return self.toolset in {FINANCE_SAFE_TOOLSET, GENERAL_FULL_TOOLSET}
        if normalized in {
            "general_read",
            "general_write",
            "general_execute",
            "browser",
            "delegation",
            "cron_admin",
            "webhook_admin",
            "web",
            "skills",
            "plugins",
            "mcp_admin",
            "model_provider",
            "memory_admin",
            "security",
            "clarify",
            "todo",
            "vision",
            "image_gen",
            "tts",
            "stt",
            "messaging",
            "platform_gateway",
            "platform_admin",
            "homeassistant",
            "rl_training",
            "moa",
            "terminal_backend",
            "learning",
            "tui_control",
        }:
            return self.toolset == GENERAL_FULL_TOOLSET and self.general_tools_enabled
        return False


class ToolPolicyEngine:
    def __init__(self, policy: ToolPolicy | None = None) -> None:
        self.policy = policy or build_policy_from_env()

    @property
    def toolset(self) -> str:
        return self.policy.toolset

    def allows(self, metadata: dict[str, object] | None) -> bool:
        info = dict(metadata or {})
        return self.policy.allows_category(str(info.get("category") or "financial_read"))

    def require_allowed(self, name: str, metadata: dict[str, object] | None) -> None:
        ensure_agent_tool_name(name)
        if not self.allows(metadata):
            category = str(dict(metadata or {}).get("category") or "financial_read")
            raise PermissionError(f"tool {name} is not allowed by toolset={self.toolset} category={category}")


def _workspace_roots_from_env() -> tuple[str, ...]:
    raw = str(os.getenv("AIASK_AGENT_WORKSPACE_ROOTS", "")).strip()
    if raw:
        roots = [item.strip() for item in raw.split(os.pathsep) if item.strip()]
    else:
        roots = [os.getcwd()]
    return tuple(roots)


def build_policy_from_env() -> ToolPolicy:
    toolset = str(os.getenv("AIASK_AGENT_TOOLSET", FINANCE_SAFE_TOOLSET)).strip() or FINANCE_SAFE_TOOLSET
    if toolset not in {FINANCE_SAFE_TOOLSET, GENERAL_FULL_TOOLSET}:
        toolset = FINANCE_SAFE_TOOLSET
    general_tools_enabled = str(os.getenv("AIASK_AGENT_ENABLE_GENERAL_TOOLS", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return ToolPolicy(
        toolset=toolset,
        general_tools_enabled=general_tools_enabled,
        workspace_roots=_workspace_roots_from_env(),
    )


def ensure_agent_tool_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized.startswith(AGENT_TOOL_PREFIX):
        raise ValueError(f"AIASK Agent tool names must use agent_* prefix: {normalized}")
    lowered = normalized.lower()
    for token in FORBIDDEN_DIRECT_MANAGER_TOKENS:
        if token in lowered:
            raise ValueError(f"AIASK Agent tool name leaks forbidden token {token!r}: {normalized}")
    return normalized


def assert_safe_catalog_names(names: Iterable[str]) -> None:
    for name in names:
        ensure_agent_tool_name(name)
