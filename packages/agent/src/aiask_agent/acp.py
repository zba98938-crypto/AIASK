from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .mcp_client import MCPAggregator


class ACPManager:
    """AIASK-native ACP management surface for client-provided MCP servers."""

    def __init__(self, *, mcp: MCPAggregator | None = None, config_path: Path | None = None) -> None:
        self.mcp = mcp or MCPAggregator(config_path=config_path)

    def status(self) -> dict[str, Any]:
        diagnostics = self.mcp.registration_diagnostics()
        servers = self.mcp.servers_summary(include_all=True)
        client_servers = [item for item in servers if str(item.get("registered_by") or "") == "acp_client"]
        return {
            "object": "aiask.acp_status",
            "implemented": True,
            "client_provided_mcp_servers": {
                "count": len(client_servers),
                "servers": client_servers,
            },
            "all_mcp_servers": servers,
            "registration": diagnostics,
            "status": "implemented",
            "secrets_redacted": True,
        }

    def register_server(self, arguments: dict[str, Any]) -> dict[str, Any]:
        server = self.mcp.register_client_server(
            name=str(arguments.get("name") or ""),
            transport=arguments.get("transport"),
            url=arguments.get("url"),
            command=arguments.get("command"),
            args=arguments.get("args"),
            domain=arguments.get("domain"),
            tools=arguments.get("tools"),
            resources=arguments.get("resources"),
            prompts=arguments.get("prompts"),
            oauth=arguments.get("oauth"),
            headers_from_env=arguments.get("headers_from_env"),
            enabled=bool(arguments.get("enabled", True)),
            description=arguments.get("description"),
        )
        return {"server": server, "status": self.status()}

    def remove_server(self, name: str) -> dict[str, Any]:
        removed = self.mcp.remove_server(name)
        return {"removed": removed, "status": self.status()}

    def readiness(self) -> dict[str, Any]:
        status = self.status()
        return {
            "configured": bool(status["all_mcp_servers"]),
            "client_server_count": status["client_provided_mcp_servers"]["count"],
            "config_path": self.mcp.registration_diagnostics().get("config_path"),
            "config_exists": os.path.exists(str(self.mcp.registration_diagnostics().get("config_path") or "")),
            "status": "implemented",
        }

