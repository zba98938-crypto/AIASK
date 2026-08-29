from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request

from ..capabilities import parity_summary
from ..gateway import ADAPTERS
from ..runtime import AgentRuntime


def create_health_router(
    *,
    runtime: AgentRuntime,
    build_full_runtime: Callable[[], AgentRuntime],
    hermes_full_enabled: Callable[[], bool],
    full_runtime_active: Callable[[], bool],
    require_api: Callable[[Request], None],
    tool_catalog_payload: Callable[[AgentRuntime], dict[str, Any]],
    desktop_capabilities_payload: Callable[[Request], Awaitable[dict[str, Any]]],
    redact_required_env: Callable[..., Any],
    parity_live_evidence: Callable[[dict[str, Any]], dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "aiask-agent"}

    @router.get("/health/detailed")
    async def health_detailed() -> dict[str, Any]:
        parity_names = build_full_runtime().tool_registry.names() if hermes_full_enabled() else runtime.tool_registry.names()
        parity = parity_summary(parity_names, env=dict(os.environ), gateway_adapters=ADAPTERS.keys())
        return {
            "status": "ok",
            "service": "aiask-agent",
            "runtime": {
                "model": runtime.model,
                "max_iterations": runtime.max_iterations,
                "model_timeout_seconds": runtime.model_timeout_seconds,
                "tool_timeout_seconds": runtime.tool_timeout_seconds,
                "server": "fastapi_asgi",
            },
            "tools": {
                "count": len(runtime.tool_registry.names()),
                "names": runtime.tool_registry.names(),
                "toolset": runtime.tool_registry.policy_engine.toolset,
            },
            "hermes": {
                "mode": "aiask_native",
                "full_mode_enabled": hermes_full_enabled(),
                "full_mode_active": full_runtime_active(),
                "parity": redact_required_env(
                    parity,
                    redact_sensitive_names=True,
                ),
                "live_evidence": redact_required_env(parity_live_evidence(parity), redact_sensitive_names=True),
            },
            "control": {
                "loopback_only": True,
                "token_configured": bool(
                    str(os.getenv("AIASK_AGENT_CONTROL_TOKEN", "")).strip()
                    or str(os.getenv("AIASK_LOCAL_CONTROL_TOKEN", "")).strip()
                ),
            },
        }

    @router.get("/v1/capabilities/parity")
    async def capabilities_parity(request: Request) -> dict[str, Any]:
        require_api(request)
        selected = build_full_runtime() if hermes_full_enabled() else runtime
        return parity_summary(selected.tool_registry.names(), env=dict(os.environ), gateway_adapters=ADAPTERS.keys())

    @router.get("/v1/tools")
    async def tools(request: Request) -> dict[str, Any]:
        require_api(request)
        return tool_catalog_payload(runtime)

    @router.get("/v1/desktop/capabilities")
    async def desktop_capabilities(request: Request) -> dict[str, Any]:
        require_api(request)
        return await desktop_capabilities_payload(request)

    return router
