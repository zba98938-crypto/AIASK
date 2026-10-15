from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .paths import aiask_agent_home
from .tool_risk import extract_mcp_action, mcp_tool_is_live_broker_trade_risk, mcp_tool_metadata, normalize_side_effect
from .tools.policy import FORBIDDEN_DIRECT_MANAGER_TOKENS
import logging

logger = logging.getLogger(__name__)

try:
    import httpx
except Exception:  # pragma: no cover - mcp remote transports require httpx at runtime.
    httpx = None  # type: ignore[assignment]

# MCP 1.x exposed ``streamablehttp_client`` while newer SDKs renamed it to
# ``streamable_http_client``.  Keep the old attribute available for plugins and
# callers that still monkeypatch the 1.x spelling, while resolving either name
# at the actual call site below.
try:  # pragma: no cover - availability depends on the installed MCP major.
    import mcp.client.streamable_http as _streamable_http

    if not hasattr(_streamable_http, "streamablehttp_client") and hasattr(_streamable_http, "streamable_http_client"):
        _streamable_http.streamablehttp_client = _streamable_http.streamable_http_client
except Exception:
    _streamable_http = None  # type: ignore[assignment]

DEFAULT_LOCAL_MCP_AUTH_HEADER = "Authorization"
MCP_TOOL_CONTRACT_FIELDS = (
    "input_schema",
    "output_schema",
    "freshness",
    "examples",
    "contract_version",
    "contract_source",
    "source_policy",
    "standard_model",
    "provider_choices",
    "provider_status",
    "quality_gate",
    "reconciliation",
    "form_schema",
)


def _financial_manager_internal_mcp_allowlist() -> dict[str, frozenset[str] | None]:
    """Return the Desktop Financial Manager internal read-only MCP allowlist.

    ``None`` means the MCP tool is a direct read-only tool with no action
    selector.  A non-empty set means the raw MCP manager is only available for
    those catalog-declared read actions.
    """

    try:
        from .financial_safety import FINANCIAL_MANAGER_ACTIONS
    except Exception:
        return {}

    allowlist: dict[str, set[str] | None] = {}
    for raw in FINANCIAL_MANAGER_ACTIONS:
        if str(raw.get("mode") or "read_only") != "read_only":
            continue
        tool_name = str(raw.get("mcp_tool") or "").strip()
        if not tool_name:
            continue
        action = str(raw.get("mcp_action") or "").strip()
        if not action:
            allowlist[tool_name] = None
            continue
        current = allowlist.setdefault(tool_name, set())
        if current is not None:
            current.add(action)
    return {
        tool_name: None if actions is None else frozenset(actions)
        for tool_name, actions in allowlist.items()
    }


def default_mcp_config_path() -> Path:
    raw = str(os.getenv("AIASK_AGENT_MCP_CONFIG", "")).strip()
    if raw:
        return Path(raw).expanduser()
    return aiask_agent_home() / "mcp_servers.json"


def default_mcp_token_dir() -> Path:
    raw = str(os.getenv("AIASK_AGENT_MCP_TOKEN_DIR", "")).strip()
    if raw:
        return Path(raw).expanduser()
    return aiask_agent_home() / "mcp-tokens"


def _safe_slug(value: str) -> str:
    token = "".join(ch if ch.isalnum() else "_" for ch in str(value or "").lower()).strip("_")
    return token or "default"


def _local_auth_header_env(server_name: str) -> str:
    return f"AIASK_MCP_{_safe_slug(server_name).upper()}_AUTHORIZATION"


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump_model(item) for item in value]
    if isinstance(value, tuple):
        return [_dump_model(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _dump_model(item) for key, item in value.items()}
    return value


def _contract_metadata_from_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {field: tool.get(field) for field in MCP_TOOL_CONTRACT_FIELDS if tool.get(field) is not None}


def _mcp_error_text(exc: BaseException) -> str:
    pieces = [str(exc)]
    for attr in ("error", "data", "message", "code"):
        value = getattr(exc, attr, None)
        if value is not None:
            pieces.append(str(value))
    nested_error = getattr(getattr(exc, "error", None), "message", None)
    if nested_error is not None:
        pieces.append(str(nested_error))
    nested_code = getattr(getattr(exc, "error", None), "code", None)
    if nested_code is not None:
        pieces.append(str(nested_code))
    return " ".join(piece for piece in pieces if piece)


def _is_mcp_method_not_found(exc: BaseException) -> bool:
    text = _mcp_error_text(exc).lower()
    return "-32601" in text or "method not found" in text


class MCPOAuthRequired(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("MCP OAuth authorization is required")
        self.payload = dict(payload)


class _BearerAuth(httpx.Auth if httpx is not None else object):  # type: ignore[misc]
    requires_request_body = False
    requires_response_body = False

    def __init__(self, token: str) -> None:
        self.token = token

    def auth_flow(self, request: Any) -> Any:
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class MCPTokenStore:
    """Small file-backed token store compatible with mcp.client.auth.TokenStorage."""

    def __init__(self, server_name: str, root: Path | None = None) -> None:
        self.server_name = str(server_name or "default")
        self.root = root or default_mcp_token_dir()
        self.path = self.root / f"{_safe_slug(self.server_name)}.json"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError as _swallowed:
            logger.warning("packages/agent/src/aiask_agent/mcp_client.py:182: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

    async def get_tokens(self) -> Any | None:
        raw = dict(self._load().get("tokens") or {})
        if not raw.get("access_token"):
            return None
        from mcp.shared.auth import OAuthToken

        return OAuthToken(**raw)

    async def set_tokens(self, tokens: Any) -> None:
        payload = self._load()
        payload["tokens"] = _dump_model(tokens)
        self._save(payload)

    async def get_client_info(self) -> Any | None:
        raw = dict(self._load().get("client_info") or {})
        if not raw:
            return None
        from mcp.shared.auth import OAuthClientInformationFull

        return OAuthClientInformationFull(**raw)

    async def set_client_info(self, client_info: Any) -> None:
        payload = self._load()
        payload["client_info"] = _dump_model(client_info)
        self._save(payload)

    def store_token_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        tokens = {
            "access_token": str(payload.get("access_token") or "").strip(),
            "token_type": str(payload.get("token_type") or "Bearer") or "Bearer",
        }
        for key in ("expires_in", "scope", "refresh_token"):
            if payload.get(key) is not None:
                tokens[key] = payload[key]
        if not tokens["access_token"]:
            raise ValueError("access_token is required")
        current = self._load()
        current["tokens"] = tokens
        self._save(current)
        return self.summary(configured=True)

    def summary(self, *, configured: bool) -> dict[str, Any]:
        raw = self._load()
        token = dict(raw.get("tokens") or {})
        return {
            "server": self.server_name,
            "configured": bool(configured),
            "token_available": bool(token.get("access_token")),
            "token_type": token.get("token_type") if token.get("access_token") else None,
            "scope": token.get("scope") if token.get("access_token") else None,
            "token_store": str(self.path),
        }


class MCPAggregator:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or default_mcp_config_path()
        self.config_error: str | None = None
        self.config = self._load_config()

    def financial_tools(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for server in self._servers():
            if str(server.get("domain") or "").lower() != "financial":
                continue
            for tool in list(server.get("tools") or []):
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                tool_name = str(tool.get("name") or "")
                if not self._is_safe_financial_surface_tool(tool):
                    continue
                side_effect = self._tool_side_effect_from_item(tool, target=tool_name)
                candidates.append(
                    {
                        "server": str(server.get("name") or ""),
                        "name": tool_name,
                        "wrapped_name": self.wrap_name(str(server.get("name") or ""), tool_name),
                        "description": str(tool.get("description") or f"MCP financial tool {tool['name']}"),
                        "parameters": dict(tool.get("parameters") or {"type": "object", "properties": {}}),
                        "side_effect": side_effect,
                        **_contract_metadata_from_tool(tool),
                    }
                )
        collisions = self._wrapped_name_collisions(candidates)
        return [item for item in candidates if item["wrapped_name"] not in collisions]

    async def call(self, wrapped_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        server_name, tool_name = self.unwrap_name(wrapped_name)
        server = next((item for item in self._servers() if item.get("name") == server_name), None)
        if not server:
            raise ValueError(f"MCP server is not configured: {server_name}")
        if str(server.get("domain") or "").lower() != "financial":
            raise PermissionError(f"MCP server is not in financial domain: {server_name}")
        allowed = {str(item.get("name")) for item in list(server.get("tools") or []) if isinstance(item, dict)}
        if tool_name not in allowed:
            raise PermissionError(f"MCP tool is not in allowlist: {server_name}.{tool_name}")
        return await self._with_session(server, lambda session: session.call_tool(tool_name, dict(arguments or {})))

    def internal_financial_tool(self, tool_name: str) -> dict[str, Any] | None:
        wanted = str(tool_name or "").strip()
        if not wanted:
            return None
        action_allowlist = _financial_manager_internal_mcp_allowlist()
        if wanted not in action_allowlist:
            return None
        for server in self._servers():
            if str(server.get("domain") or "").lower() != "financial":
                continue
            for tool in list(server.get("tools") or []):
                if not isinstance(tool, dict) or str(tool.get("name") or "").strip() != wanted:
                    continue
                if mcp_tool_is_live_broker_trade_risk(tool) or self._looks_like_general_tool(wanted, str(tool.get("description") or "")):
                    return None
                allowed_actions = action_allowlist[wanted]
                return {
                    "server": str(server.get("name") or ""),
                    "name": wanted,
                    "wrapped_name": self.wrap_name(str(server.get("name") or ""), wanted),
                    "description": str(tool.get("description") or f"MCP financial tool {wanted}"),
                    "parameters": dict(tool.get("parameters") or {"type": "object", "properties": {}}),
                    "side_effect": tool.get("side_effect"),
                    "internal_desktop_route": True,
                    "allowed_actions": sorted(allowed_actions) if allowed_actions is not None else None,
                    **_contract_metadata_from_tool(tool),
                }
        return None

    async def call_internal_financial_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.internal_financial_tool(tool_name)
        if not tool:
            raise PermissionError(f"MCP financial tool is not available for internal Desktop use: {tool_name}")
        allowed_actions = tool.get("allowed_actions")
        if isinstance(allowed_actions, list):
            action = extract_mcp_action(arguments)
            if action not in {str(item) for item in allowed_actions}:
                suffix = f".{action}" if action else ""
                raise PermissionError(
                    f"MCP financial action is not available for internal Desktop use: {tool_name}{suffix}"
                )
        server = self._server_by_name(str(tool["server"]))
        return await self._with_session(server, lambda session: session.call_tool(str(tool["name"]), dict(arguments or {})))

    def servers_summary(self, *, include_all: bool = False) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        unique_wrapped_names = {item["wrapped_name"] for item in self.financial_tools()}
        for server in self._servers():
            financial_domain = str(server.get("domain") or "").lower() == "financial"
            if not (include_all or financial_domain):
                continue
            raw_tools = [tool for tool in list(server.get("tools") or []) if isinstance(tool, dict)]
            safe_financial_tools = [
                str(tool.get("name") or "")
                for tool in raw_tools
                if financial_domain
                and tool.get("name")
                and self._is_safe_financial_surface_tool(tool)
                and self.wrap_name(str(server.get("name") or ""), str(tool.get("name") or "")) in unique_wrapped_names
            ]
            trade_risk_tools_blocked = [
                str(tool.get("name") or "")
                for tool in raw_tools
                if tool.get("name") and mcp_tool_is_live_broker_trade_risk(tool)
            ]
            auth = self._auth_readiness(server)
            row = {
                "name": server.get("name"),
                "domain": server.get("domain"),
                "transport": server.get("transport", "stdio"),
                "url": server.get("url"),
                "registered_by": server.get("registered_by"),
                "tools": [
                    tool.get("name")
                    for tool in raw_tools
                    if include_all
                    or (
                        self._is_safe_financial_surface_tool(tool)
                        and self.wrap_name(str(server.get("name") or ""), str(tool.get("name") or "")) in unique_wrapped_names
                    )
                ],
                "resources_enabled": bool(server.get("resources")),
                "resources": list(server.get("resources") or []) if include_all else [],
                "prompts_enabled": bool(server.get("prompts")),
                "prompts": list(server.get("prompts") or []) if include_all else [],
                "partial_success": bool(server.get("partial_success")),
                "warnings": list(server.get("warnings") or []) if include_all else [],
                "unsupported_methods": list(server.get("unsupported_methods") or []),
                "oauth_configured": bool(server.get("oauth") or server.get("auth") == "oauth"),
                "oauth_token_available": MCPTokenStore(str(server.get("name") or "")).summary(configured=self._oauth_configured(server))[
                    "token_available"
                ],
                "auth_configured": auth["auth_configured"],
                "auth_mode": auth["auth_mode"],
                "auth_env_vars": auth["auth_env_vars"],
                "missing_auth_env_vars": auth["missing_auth_env_vars"],
            }
            if financial_domain or trade_risk_tools_blocked:
                row.update(
                    {
                        "safe_financial_tools": safe_financial_tools,
                        "trade_risk_tools_blocked": trade_risk_tools_blocked,
                        "live_trading_enabled": False,
                        "live_trading_policy": "blocked_in_agent_mcp_surface",
                    }
                )
            items.append(
                row
            )
        return items

    def tools_summary(self, *, include_all: bool = False) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        unique_wrapped_names = {item["wrapped_name"] for item in self.financial_tools()}
        for server in self._servers():
            if not include_all and str(server.get("domain") or "").lower() != "financial":
                continue
            for tool in list(server.get("tools") or []):
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                wrapped_name = self.wrap_name(str(server.get("name") or ""), str(tool.get("name") or ""))
                if not include_all and (
                    not self._is_safe_financial_surface_tool(tool)
                    or wrapped_name not in unique_wrapped_names
                ):
                    continue
                side_effect = self._tool_side_effect_from_item(tool, target=str(tool.get("name") or ""))
                tool_payload = {**tool, "server": server.get("name"), "side_effect": side_effect}
                if mcp_tool_is_live_broker_trade_risk(tool_payload):
                    side_effect = mcp_tool_metadata(tool_payload).get("side_effect")
                items.append(
                    {
                        "server": server.get("name"),
                        "domain": server.get("domain"),
                        "transport": server.get("transport", "stdio"),
                        "name": tool.get("name"),
                        "wrapped_name": wrapped_name,
                        "description": tool.get("description"),
                        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                        "side_effect": side_effect,
                        **_contract_metadata_from_tool(tool),
                    }
                )
        return items

    def resources_summary(self, *, include_all: bool = False) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        for server in self._servers():
            if not include_all and str(server.get("domain") or "").lower() != "financial":
                continue
            for resource in list(server.get("resources") or []):
                if isinstance(resource, dict):
                    resources.append({"server": server.get("name"), **resource})
                else:
                    resources.append({"server": server.get("name"), "uri": str(resource)})
        return resources

    def prompts_summary(self, *, include_all: bool = False) -> list[dict[str, Any]]:
        prompts: list[dict[str, Any]] = []
        for server in self._servers():
            if not include_all and str(server.get("domain") or "").lower() != "financial":
                continue
            for prompt in list(server.get("prompts") or []):
                if isinstance(prompt, dict):
                    prompts.append({"server": server.get("name"), **prompt})
                else:
                    prompts.append({"server": server.get("name"), "name": str(prompt)})
        return prompts

    def oauth_status(self, *, include_all: bool = False) -> list[dict[str, Any]]:
        items = []
        for server in self._servers():
            if not include_all and str(server.get("domain") or "").lower() != "financial":
                continue
            configured = self._oauth_configured(server)
            oauth = self._oauth_config(server)
            summary = MCPTokenStore(str(server.get("name") or "")).summary(configured=configured)
            summary.update(
                {
                    "server": server.get("name"),
                    "domain": server.get("domain"),
                    "transport": server.get("transport", "stdio"),
                    "authorization_url_available": bool(
                        oauth.get("authorization_url") or oauth.get("authorize_url") or oauth.get("auth_url")
                    ),
                    "token_url_available": bool(oauth.get("token_url") or oauth.get("token_endpoint")),
                }
            )
            items.append(summary)
        return items

    def registration_diagnostics(self) -> dict[str, Any]:
        mcp_port = str(os.getenv("MCP_PORT", "")).strip()
        servers = self._servers()
        config_exists = self.config_path.exists()
        detected_url = f"http://127.0.0.1:{mcp_port}/mcp" if mcp_port else None
        auth_env_vars: list[str] = []
        missing_auth_env_vars: list[str] = []
        for server in servers:
            auth = self._auth_readiness(server)
            auth_env_vars.extend(str(item) for item in auth["auth_env_vars"])
            missing_auth_env_vars.extend(str(item) for item in auth["missing_auth_env_vars"])
        auth_env_vars = list(dict.fromkeys(auth_env_vars))
        missing_auth_env_vars = list(dict.fromkeys(missing_auth_env_vars))
        if self.config_error:
            status = "invalid_config"
            error_code = "MCP_CONFIG_INVALID"
            configured = False
        elif config_exists and servers:
            status = "registered"
            error_code = None
            configured = True
        elif config_exists:
            status = "empty_config"
            error_code = "MCP_CONFIG_EMPTY"
            configured = False
        elif mcp_port:
            status = "MCP_SERVICE_RUNNING_BUT_NOT_REGISTERED"
            error_code = status
            configured = False
        else:
            status = "not_registered"
            error_code = "MCP_AGGREGATOR_CONFIG_MISSING"
            configured = False
        discovered_counts = {
            "tools": sum(len(list(server.get("tools") or [])) for server in servers),
            "resources": sum(len(list(server.get("resources") or [])) for server in servers),
            "prompts": sum(len(list(server.get("prompts") or [])) for server in servers),
        }
        warnings = [warning for server in servers for warning in list(server.get("warnings") or []) if isinstance(warning, dict)]
        unsupported_methods = list(
            dict.fromkeys(
                str(method)
                for server in servers
                for method in list(server.get("unsupported_methods") or [])
                if str(method or "").strip()
            )
        )
        if status not in {"registered"}:
            discovery_status = status
        elif missing_auth_env_vars:
            discovery_status = "auth_missing"
        elif any(discovered_counts.values()):
            discovery_status = "discovered"
        else:
            discovery_status = "registered"
        return {
            "configured": configured,
            "registration_status": status,
            "discovery_status": discovery_status,
            "discovered_counts": discovered_counts,
            "partial_success": bool(warnings),
            "warnings": warnings,
            "unsupported_methods": unsupported_methods,
            "error_code": error_code,
            "config_path": str(self.config_path),
            "config_exists": config_exists,
            "server_count": len(servers),
            "detected_service_port": mcp_port or None,
            "detected_service_url": detected_url,
            "suggested_registration_url": detected_url,
            "auth_configured": not missing_auth_env_vars,
            "auth_env_vars": auth_env_vars,
            "missing_auth_env_vars": missing_auth_env_vars,
            "detail": self.config_error
            or (
                f"MCP service port {mcp_port} is set, but AIASK_AGENT_MCP_CONFIG or {self.config_path} is required for AIASK aggregation."
                if status == "MCP_SERVICE_RUNNING_BUT_NOT_REGISTERED"
                else None
            ),
        }

    def register_local_server(
        self,
        *,
        name: str | None = None,
        url: str | None = None,
        transport: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        diagnostics = self.registration_diagnostics()
        server_name = _safe_slug(name or "akshare-local").replace("_", "-")
        final_url = str(url or diagnostics.get("suggested_registration_url") or "").strip()
        if not final_url:
            raise ValueError("MCP local server url is required")
        final_transport = str(transport or "streamable_http").strip().lower().replace("-", "_")
        if final_transport == "http":
            final_transport = "streamable_http"
        server = {
            "name": server_name,
            "domain": str(domain or "financial").strip() or "financial",
            "transport": final_transport,
            "url": final_url,
            "headers_from_env": {
                DEFAULT_LOCAL_MCP_AUTH_HEADER: _local_auth_header_env(server_name),
            },
            "tools": [],
            "resources": [],
            "prompts": [],
            "enabled": True,
            "description": "Local MCP service registered by AIASK Desktop",
        }
        self._upsert_server(server)
        return {
            "server": server,
            "registration": self.registration_diagnostics(),
        }

    def register_client_server(
        self,
        *,
        name: str,
        transport: str | None = None,
        url: str | None = None,
        command: str | None = None,
        args: Any = None,
        domain: str | None = None,
        tools: Any = None,
        resources: Any = None,
        prompts: Any = None,
        oauth: Any = None,
        headers_from_env: Any = None,
        enabled: bool = True,
        description: str | None = None,
    ) -> dict[str, Any]:
        if not str(name or "").strip():
            raise ValueError("MCP client server name is required")
        server_name = _safe_slug(name).replace("_", "-")
        final_transport = str(transport or ("streamable_http" if url else "stdio")).strip().lower().replace("-", "_")
        if final_transport == "http":
            final_transport = "streamable_http"
        server: dict[str, Any] = {
            "name": server_name,
            "domain": str(domain or "financial").strip().lower() or "financial",
            "transport": final_transport,
            "enabled": bool(enabled),
            "registered_by": "acp_client",
            "description": description or "Client-provided MCP server registered through AIASK ACP",
            "tools": self._normalize_tool_items(tools),
            "resources": self._normalize_named_items(resources, key="uri"),
            "prompts": self._normalize_named_items(prompts, key="name"),
        }
        if url:
            server["url"] = str(url).strip()
        if command:
            server["command"] = str(command).strip()
            server["args"] = [str(item) for item in list(args or [])]
        if isinstance(headers_from_env, dict):
            server["headers_from_env"] = {str(key): str(value) for key, value in headers_from_env.items() if str(key).strip() and str(value).strip()}
        if isinstance(oauth, dict):
            server["oauth"] = dict(oauth)
        elif oauth:
            server["auth"] = "oauth"
        if final_transport in {"streamable_http", "sse"} and not server.get("url"):
            raise ValueError("url is required for HTTP/SSE MCP servers")
        if final_transport == "stdio" and not server.get("command"):
            raise ValueError("command is required for stdio MCP servers")
        self._upsert_server(server)
        return server

    def remove_server(self, name: str) -> bool:
        wanted = str(name or "").strip()
        if not wanted:
            raise ValueError("MCP server name is required")
        servers = self._servers()
        next_servers = [item for item in servers if str(item.get("name") or "") != wanted]
        removed = len(next_servers) != len(servers)
        if removed:
            self._set_servers(next_servers)
            self._save_config()
        return removed

    def auth_readiness(self, server_name: str | None = None) -> dict[str, Any]:
        if server_name:
            return self._auth_readiness(self._server_by_name(server_name))
        diagnostics = self.registration_diagnostics()
        return {
            "auth_configured": diagnostics["auth_configured"],
            "auth_env_vars": diagnostics["auth_env_vars"],
            "missing_auth_env_vars": diagnostics["missing_auth_env_vars"],
        }

    async def discover_and_update(self, server_name: str) -> dict[str, Any]:
        discovered = await self.discover(server_name)
        server = self._server_by_name(server_name)
        updated = dict(server)
        tools = self._normalize_tool_items(discovered.get("tools"))
        resources = self._normalize_named_items(discovered.get("resources"), key="uri")
        prompts = self._normalize_named_items(discovered.get("prompts"), key="name")
        updated["tools"] = tools
        updated["resources"] = resources
        updated["prompts"] = prompts
        updated["partial_success"] = bool(discovered.get("partial_success"))
        updated["warnings"] = list(discovered.get("warnings") or [])
        updated["unsupported_methods"] = list(discovered.get("unsupported_methods") or [])
        self._upsert_server(updated)
        return {
            "server": updated.get("name"),
            "tools_count": len(tools),
            "resources_count": len(resources),
            "prompts_count": len(prompts),
            "discovered": discovered,
            "partial_success": bool(discovered.get("partial_success")),
            "warnings": list(discovered.get("warnings") or []),
            "unsupported_methods": list(discovered.get("unsupported_methods") or []),
            "registration": self.registration_diagnostics(),
        }

    async def discover(self, server_name: str) -> dict[str, Any]:
        server = self._server_by_name(server_name)

        async def operation(session: Any) -> dict[str, Any]:
            tools = await session.list_tools()
            warnings: list[dict[str, Any]] = []
            unsupported_methods: list[str] = []

            async def optional_list(method_name: str, call: Any, result_attr: str) -> Any:
                try:
                    result = await call()
                except Exception as exc:
                    if not _is_mcp_method_not_found(exc):
                        raise
                    detail = str(exc) or _mcp_error_text(exc)
                    unsupported_methods.append(method_name)
                    warnings.append(
                        {
                            "method": method_name,
                            "status": "unsupported",
                            "detail": detail,
                        }
                    )
                    return []
                return getattr(result, result_attr, result)

            resources = await optional_list("resources/list", session.list_resources, "resources")
            prompts = await optional_list("prompts/list", session.list_prompts, "prompts")
            return {
                "server": server.get("name"),
                "transport": self._transport(server),
                "tools": _dump_model(getattr(tools, "tools", tools)),
                "resources": _dump_model(resources),
                "prompts": _dump_model(prompts),
                "partial_success": bool(warnings),
                "warnings": warnings,
                "unsupported_methods": unsupported_methods,
            }

        return await self._with_session(server, operation)

    async def read_resource(self, server_name: str, uri: str) -> dict[str, Any]:
        server = self._server_by_name(server_name)
        if not str(uri or "").strip():
            raise ValueError("uri is required")
        result = await self._with_session(server, lambda session: session.read_resource(str(uri)))
        return {"server": server.get("name"), "uri": uri, "result": result}

    async def get_prompt(self, server_name: str, prompt_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        server = self._server_by_name(server_name)
        if not str(prompt_name or "").strip():
            raise ValueError("prompt name is required")
        result = await self._with_session(
            server,
            lambda session: session.get_prompt(str(prompt_name), {str(k): str(v) for k, v in dict(arguments or {}).items()} or None),
        )
        return {"server": server.get("name"), "name": prompt_name, "result": result}

    def oauth_start(self, server_name: str, *, redirect_uri: str | None = None, scope: str | None = None) -> dict[str, Any]:
        server = self._server_by_name(server_name)
        payload = self._oauth_required_payload(server, redirect_uri=redirect_uri, scope=scope)
        payload["status"] = "already_authorized" if payload.get("token_available") else "oauth_required"
        return payload

    def oauth_callback(self, server_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        server = self._server_by_name(server_name)
        if not self._oauth_configured(server):
            raise ValueError(f"MCP server does not use OAuth: {server_name}")
        data = dict(payload or {})
        if not data.get("access_token") and data.get("code"):
            data = self._exchange_oauth_code(server, data)
        summary = MCPTokenStore(str(server.get("name") or "")).store_token_payload(data)
        summary.update({"status": "stored", "domain": server.get("domain"), "transport": self._transport(server)})
        return summary

    @staticmethod
    def wrap_name(server_name: str, tool_name: str) -> str:
        safe_server = "".join(ch if ch.isalnum() else "_" for ch in server_name.lower()).strip("_")
        safe_tool = "".join(ch if ch.isalnum() else "_" for ch in tool_name.lower()).strip("_")
        return f"agent_mcp_{safe_server}_{safe_tool}"

    @staticmethod
    def _wrapped_name_collisions(tools: list[dict[str, Any]]) -> set[str]:
        counts: dict[str, int] = {}
        for tool in tools:
            wrapped = str(tool.get("wrapped_name") or "")
            if wrapped:
                counts[wrapped] = counts.get(wrapped, 0) + 1
        return {name for name, count in counts.items() if count > 1}

    @staticmethod
    def _is_forbidden_direct_tool(tool_name: str) -> bool:
        lowered = str(tool_name or "").strip().lower()
        return lowered.endswith("_manager") or any(token in lowered for token in FORBIDDEN_DIRECT_MANAGER_TOKENS)

    @classmethod
    def _is_safe_financial_surface_tool(cls, tool: dict[str, Any]) -> bool:
        tool_name = str(tool.get("name") or "").strip()
        if not tool_name:
            return False
        description = str(tool.get("description") or "")
        side_effect = cls._tool_side_effect_from_item(tool, target=tool_name)
        if side_effect is not None and normalize_side_effect(side_effect, target=tool_name).get("level") != "read_only":
            return False
        return not (
            cls._is_forbidden_direct_tool(tool_name)
            or cls._looks_like_general_tool(tool_name, description)
            or mcp_tool_is_live_broker_trade_risk(tool)
        )

    def unwrap_name(self, wrapped_name: str) -> tuple[str, str]:
        for item in self.financial_tools():
            if item["wrapped_name"] == wrapped_name:
                return item["server"], item["name"]
        raise ValueError(f"MCP wrapped tool is not configured: {wrapped_name}")

    def _servers(self) -> list[dict[str, Any]]:
        raw = self.config.get("servers", [])
        if isinstance(raw, dict):
            values = []
            for name, item in raw.items():
                if isinstance(item, dict):
                    values.append({"name": name, **item})
            return values
        return [item for item in raw if isinstance(item, dict)]

    def _set_servers(self, servers: list[dict[str, Any]]) -> None:
        self.config["servers"] = servers

    def _save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        try:
            self.config_path.chmod(0o600)
        except OSError as _swallowed:
            logger.warning("packages/agent/src/aiask_agent/mcp_client.py:825: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

    def _upsert_server(self, server: dict[str, Any]) -> None:
        servers = self._servers()
        wanted = str(server.get("name") or "").strip()
        if not wanted:
            raise ValueError("MCP server name is required")
        replaced = False
        next_servers: list[dict[str, Any]] = []
        for item in servers:
            if str(item.get("name") or "").strip() == wanted:
                next_servers.append({**item, **server})
                replaced = True
            else:
                next_servers.append(item)
        if not replaced:
            next_servers.append(server)
        self._set_servers(next_servers)
        self._save_config()

    @staticmethod
    def _normalize_tool_items(value: Any) -> list[dict[str, Any]]:
        items = list(value or []) if isinstance(value, list) else []
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            schema = item.get("inputSchema") or item.get("input_schema") or item.get("parameters") or {"type": "object", "properties": {}}
            payload = {
                "name": name,
                "description": item.get("description") or "",
                "parameters": schema,
                **_contract_metadata_from_tool(item),
            }
            side_effect = MCPAggregator._tool_side_effect_from_item(item, target=name)
            if side_effect is not None:
                payload["side_effect"] = side_effect
            normalized.append(payload)
        return normalized

    @staticmethod
    def _tool_side_effect_from_item(item: dict[str, Any], *, target: str) -> Any | None:
        if item.get("side_effect") is not None:
            return item.get("side_effect")
        if item.get("sideEffect") is not None:
            return item.get("sideEffect")
        annotations = item.get("annotations")
        if not isinstance(annotations, dict):
            return None
        if annotations.get("destructiveHint") is True:
            return {
                "level": "stateful",
                "target": target,
                "confirmation_required": True,
                "idempotent": bool(annotations.get("idempotentHint", False)),
            }
        if annotations.get("readOnlyHint") is True:
            return {
                "level": "read_only",
                "target": target,
                "confirmation_required": False,
                "idempotent": True,
            }
        return None

    @staticmethod
    def _normalize_named_items(value: Any, *, key: str) -> list[dict[str, Any]]:
        items = list(value or []) if isinstance(value, list) else []
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                candidate = str(item.get(key) or item.get("uri") or item.get("name") or "").strip()
                if candidate:
                    normalized.append({**item, key: candidate})
            elif str(item or "").strip():
                normalized.append({key: str(item).strip()})
        return normalized

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"servers": []}
        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.config_error = str(exc)
            return {"servers": []}
        if not isinstance(loaded, dict):
            self.config_error = "MCP config root must be a JSON object"
            return {"servers": []}
        return loaded

    def _server_by_name(self, server_name: str) -> dict[str, Any]:
        wanted = str(server_name or "").strip()
        server = next((item for item in self._servers() if item.get("name") == wanted), None)
        if not server:
            raise ValueError(f"MCP server is not configured: {wanted}")
        return server

    @staticmethod
    def _looks_like_general_tool(name: str, description: str = "") -> bool:
        text = f"{name} {description}".lower()
        blocked = ("terminal", "shell", "file", "browser", "code", "execution", "exec")
        return any(token in text for token in blocked)

    @staticmethod
    def _transport(server: dict[str, Any]) -> str:
        transport = str(server.get("transport") or "stdio").strip().lower().replace("-", "_")
        if transport == "http":
            return "streamable_http"
        return transport

    @staticmethod
    def _oauth_configured(server: dict[str, Any]) -> bool:
        return bool(server.get("oauth") or str(server.get("auth") or "").strip().lower() == "oauth")

    @staticmethod
    def _oauth_config(server: dict[str, Any]) -> dict[str, Any]:
        oauth = server.get("oauth")
        if isinstance(oauth, dict):
            return dict(oauth)
        return {}

    @classmethod
    def _headers(cls, server: dict[str, Any]) -> dict[str, str] | None:
        headers = {str(k): str(v) for k, v in dict(server.get("headers") or {}).items()}
        for header, env_name in cls._effective_headers_from_env(server).items():
            value = str(os.getenv(env_name, "")).strip()
            if value:
                headers[str(header)] = value
        return headers or None

    @staticmethod
    def _headers_from_env(server: dict[str, Any]) -> dict[str, str]:
        raw = server.get("headers_from_env") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(header): str(env_name).strip() for header, env_name in raw.items() if str(env_name or "").strip()}

    @classmethod
    def _effective_headers_from_env(cls, server: dict[str, Any]) -> dict[str, str]:
        headers = cls._headers_from_env(server)
        if headers:
            return headers
        if cls._is_local_mcp_http_server(server):
            return {DEFAULT_LOCAL_MCP_AUTH_HEADER: _local_auth_header_env(str(server.get("name") or "akshare-local"))}
        return {}

    @staticmethod
    def _is_local_mcp_http_server(server: dict[str, Any]) -> bool:
        url = str(server.get("url") or "").lower()
        transport = str(server.get("transport") or "").lower().replace("-", "_")
        return transport in {"streamable_http", "http", "sse"} and "/mcp" in url and ("127.0.0.1" in url or "localhost" in url)

    @staticmethod
    def _bearer_token_env(server: dict[str, Any]) -> str | None:
        if str(server.get("auth") or "").strip().lower() != "bearer_env":
            return None
        token_env = str(server.get("token_env") or "").strip()
        return token_env or None

    @classmethod
    def _auth_readiness(cls, server: dict[str, Any]) -> dict[str, Any]:
        header_envs = list(cls._effective_headers_from_env(server).values())
        token_env = cls._bearer_token_env(server)
        env_vars = list(dict.fromkeys([*header_envs, *([token_env] if token_env else [])]))
        missing = [env_name for env_name in env_vars if not str(os.getenv(env_name, "")).strip()]
        oauth_configured = cls._oauth_configured(server)
        oauth_token_available = MCPTokenStore(str(server.get("name") or "")).summary(configured=oauth_configured)["token_available"]
        if oauth_configured:
            mode = "oauth"
            configured = bool(oauth_token_available)
        elif env_vars:
            mode = "env"
            configured = not missing
        else:
            mode = "none"
            configured = True
        return {
            "auth_configured": configured,
            "auth_mode": mode,
            "auth_env_vars": env_vars,
            "missing_auth_env_vars": missing,
            "oauth_token_available": oauth_token_available,
        }

    async def _with_session(self, server: dict[str, Any], operation: Any) -> dict[str, Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as exc:
            raise RuntimeError("mcp package is required for AIASK Agent MCP aggregation") from exc

        async with AsyncExitStack() as stack:
            transport = self._transport(server)
            if transport == "stdio":
                command = str(server.get("command") or "").strip()
                if not command:
                    raise ValueError("MCP stdio server command is required")
                params = StdioServerParameters(
                    command=command,
                    args=[str(item) for item in list(server.get("args") or [])],
                    env={str(k): str(v) for k, v in dict(server.get("env") or {}).items()} or None,
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            elif transport == "sse":
                from mcp.client.sse import sse_client

                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(
                        self._server_url(server),
                        headers=self._headers(server),
                        timeout=float(server.get("timeout") or 5),
                        sse_read_timeout=float(server.get("sse_read_timeout") or 300),
                        auth=await self._auth_for_server(server),
                    )
                )
            elif transport == "streamable_http":
                from mcp.client import streamable_http as streamable_http_module

                streamablehttp_client = getattr(streamable_http_module, "streamablehttp_client", None)
                if streamablehttp_client is None:
                    streamablehttp_client = getattr(streamable_http_module, "streamable_http_client", None)
                if streamablehttp_client is None:
                    raise RuntimeError("installed MCP SDK has no Streamable HTTP client")
                auth = await self._auth_for_server(server)
                # MCP 1.x accepted headers/auth/timeout directly. MCP 2.x
                # moved those options onto its httpx2 client object.
                import inspect

                if "headers" in inspect.signature(streamablehttp_client).parameters:
                    streams = await stack.enter_async_context(
                        streamablehttp_client(
                            self._server_url(server),
                            headers=self._headers(server),
                            timeout=float(server.get("timeout") or 30),
                            sse_read_timeout=float(server.get("sse_read_timeout") or 300),
                            auth=auth,
                        )
                    )
                else:
                    try:
                        import httpx2
                    except Exception as exc:  # pragma: no cover - only MCP 2.x
                        raise RuntimeError("MCP 2.x requires the httpx2 transport dependency") from exc
                    headers = dict(self._headers(server) or {})
                    token = getattr(auth, "token", None)
                    if token and not any(key.lower() == "authorization" for key in headers):
                        headers["Authorization"] = f"Bearer {token}"
                    client = httpx2.AsyncClient(
                        headers=headers or None,
                        timeout=float(server.get("timeout") or 30),
                    )
                    client = await stack.enter_async_context(client)
                    streams = await stack.enter_async_context(
                        streamablehttp_client(self._server_url(server), http_client=client)
                    )
                read_stream, write_stream = streams[0], streams[1]
            else:
                raise NotImplementedError(f"unsupported MCP transport: {transport}")
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            result = await operation(session)
            return _dump_model(result)

    async def _auth_for_server(self, server: dict[str, Any]) -> Any | None:
        if not self._oauth_configured(server):
            token_env = self._bearer_token_env(server)
            if token_env:
                token = str(os.getenv(token_env, "")).strip()
                if not token:
                    raise PermissionError(f"MCP bearer token env {token_env} is required")
                return _BearerAuth(token)
            return None
        tokens = await MCPTokenStore(str(server.get("name") or "")).get_tokens()
        access_token = getattr(tokens, "access_token", None) if tokens is not None else None
        if not access_token:
            raise MCPOAuthRequired(self._oauth_required_payload(server))
        return _BearerAuth(str(access_token))

    @staticmethod
    def _server_url(server: dict[str, Any]) -> str:
        url = str(server.get("url") or "").strip()
        if not url:
            raise ValueError("MCP remote server url is required")
        return url

    def _oauth_required_payload(
        self,
        server: dict[str, Any],
        *,
        redirect_uri: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        oauth = self._oauth_config(server)
        store = MCPTokenStore(str(server.get("name") or ""))
        status = store.summary(configured=self._oauth_configured(server))
        authorization_url = str(oauth.get("authorization_url") or oauth.get("authorize_url") or oauth.get("auth_url") or "").strip()
        params: dict[str, str] = {}
        client_id = str(oauth.get("client_id") or "").strip()
        if client_id:
            params["client_id"] = client_id
        final_redirect = str(redirect_uri or oauth.get("redirect_uri") or "").strip()
        if final_redirect:
            params["redirect_uri"] = final_redirect
        final_scope = str(scope or oauth.get("scope") or "").strip()
        if final_scope:
            params["scope"] = final_scope
        if authorization_url and params:
            separator = "&" if "?" in authorization_url else "?"
            authorization_url = f"{authorization_url}{separator}{urlencode(params)}"
        status.update(
            {
                "server": server.get("name"),
                "domain": server.get("domain"),
                "transport": self._transport(server),
                "authorization_url": authorization_url or None,
                "redirect_uri": final_redirect or None,
                "scope": final_scope or None,
            }
        )
        return status

    def _exchange_oauth_code(self, server: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        oauth = self._oauth_config(server)
        token_url = str(oauth.get("token_url") or oauth.get("token_endpoint") or "").strip()
        if not token_url:
            raise ValueError("token_url is required to exchange OAuth code")
        form = {
            "grant_type": "authorization_code",
            "code": str(payload.get("code") or ""),
            "client_id": str(payload.get("client_id") or oauth.get("client_id") or ""),
        }
        client_secret = str(payload.get("client_secret") or oauth.get("client_secret") or "").strip()
        redirect_uri = str(payload.get("redirect_uri") or oauth.get("redirect_uri") or "").strip()
        if client_secret:
            form["client_secret"] = client_secret
        if redirect_uri:
            form["redirect_uri"] = redirect_uri
        body = urlencode({key: value for key, value in form.items() if value}).encode("utf-8")
        request = Request(token_url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urlopen(request, timeout=float(oauth.get("timeout") or 30)) as response:
            return json.loads(response.read().decode("utf-8"))
