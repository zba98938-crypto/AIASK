"""FastMCP HTTP auth and transport-security wiring."""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send


def _parse_csv(raw: str | None) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _is_http_transport_requested() -> bool:
    return str(os.getenv("MCP_TRANSPORT", "stdio")).strip().lower() in {"http", "streamable-http", "streamable_http", "sse"}


def _default_allowed_hosts(host: str, port: int) -> list[str]:
    host = str(host or "127.0.0.1").strip() or "127.0.0.1"
    port = int(port)
    candidates = {
        host,
        f"{host}:{port}",
    }
    if host in {"127.0.0.1", "localhost", "::1"}:
        candidates.update(
            {
                "127.0.0.1",
                f"127.0.0.1:{port}",
                "localhost",
                f"localhost:{port}",
                "::1",
                f"[::1]:{port}",
            }
        )
    return sorted(candidates)


def _derive_public_base_url(host: str, port: int) -> str:
    scheme = str(os.getenv("MCP_PUBLIC_SCHEME", "http")).strip() or "http"
    public_host = str(os.getenv("MCP_PUBLIC_HOST", host)).strip() or str(host)
    public_port = int(str(os.getenv("MCP_PUBLIC_PORT", port)).strip() or int(port))
    port_suffix = ""
    if not ((scheme == "http" and public_port == 80) or (scheme == "https" and public_port == 443)):
        port_suffix = f":{public_port}"
    return f"{scheme}://{public_host}{port_suffix}"


def _normalize_scopes(raw: Any, fallback: list[str]) -> list[str]:
    if isinstance(raw, list):
        scopes = [str(item).strip() for item in raw if str(item).strip()]
        return scopes or list(fallback)
    if isinstance(raw, str):
        scopes = _parse_csv(raw)
        return scopes or list(fallback)
    return list(fallback)


def _build_access_token(
    token: str,
    payload: Any,
    *,
    default_client_id: str,
    default_scopes: list[str],
    default_resource: str,
) -> AccessToken:
    client_id = default_client_id
    scopes = list(default_scopes)
    expires_at = None
    resource = default_resource

    if isinstance(payload, str) and payload.strip():
        client_id = payload.strip()
    elif isinstance(payload, dict):
        client_id = str(payload.get("client_id") or payload.get("user_id") or default_client_id).strip() or default_client_id
        scopes = _normalize_scopes(payload.get("scopes"), default_scopes)
        expires_raw = payload.get("expires_at")
        if expires_raw not in (None, ""):
            try:
                expires_at = int(float(expires_raw))
            except Exception:
                expires_at = None
        resource = str(payload.get("resource") or default_resource).strip() or default_resource

    return AccessToken(
        token=token,
        client_id=client_id,
        scopes=scopes,
        expires_at=expires_at,
        resource=resource,
    )


def _load_static_token_records(auth_mode: str, base_url: str, required_scopes: list[str]) -> dict[str, AccessToken]:
    default_client_id = str(os.getenv("MCP_AUTH_CLIENT_ID", "akshare-mcp-client")).strip() or "akshare-mcp-client"
    default_scopes = _normalize_scopes(os.getenv("MCP_AUTH_DEFAULT_SCOPES"), required_scopes or ["mcp"])

    tokens: dict[str, AccessToken] = {}
    raw_map = (
        os.getenv("MCP_AUTH_TOKEN_MAP_JSON")
        or os.getenv("MCP_AUTH_TOKENS_JSON")
        or (os.getenv("MCP_API_KEYS_JSON") if auth_mode == "api-key" else None)
    )
    if raw_map:
        try:
            parsed = json.loads(raw_map)
        except Exception as exc:
            raise RuntimeError(f"Invalid MCP_AUTH_TOKEN_MAP_JSON/MCP_AUTH_TOKENS_JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("MCP_AUTH_TOKEN_MAP_JSON/MCP_AUTH_TOKENS_JSON must be a JSON object")
        for token, payload in parsed.items():
            token_text = str(token or "").strip()
            if not token_text:
                continue
            tokens[token_text] = _build_access_token(
                token_text,
                payload,
                default_client_id=default_client_id,
                default_scopes=default_scopes,
                default_resource=base_url,
            )

    single_token = (
        os.getenv("MCP_AUTH_TOKEN")
        or os.getenv("MCP_BEARER_TOKEN")
        or (os.getenv("MCP_API_KEY") if auth_mode == "api-key" else None)
    )
    if single_token:
        token_text = str(single_token).strip()
        if token_text:
            tokens[token_text] = _build_access_token(
                token_text,
                {
                    "client_id": default_client_id,
                    "scopes": default_scopes,
                    "resource": base_url,
                },
                default_client_id=default_client_id,
                default_scopes=default_scopes,
                default_resource=base_url,
            )

    return tokens


class StaticTokenVerifier(TokenVerifier):
    """Simple TokenVerifier backed by static tokens from environment variables."""

    def __init__(self, tokens: dict[str, AccessToken]):
        self._tokens = dict(tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        token_text = str(token or "").strip()
        if not token_text:
            return None
        return self._tokens.get(token_text)


class ApiKeyHeaderToBearerMiddleware:
    """Map X-API-Key headers to Authorization Bearer for FastMCP auth middleware."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers") or [])
        header_map = {key.lower(): value for key, value in headers}
        has_authorization = b"authorization" in header_map
        api_key = header_map.get(b"x-api-key") or header_map.get(b"x-mcp-api-key")

        if not has_authorization and api_key:
            next_scope = dict(scope)
            next_scope["headers"] = [*headers, (b"authorization", b"Bearer " + api_key)]
            await self.app(next_scope, receive, send)
            return

        await self.app(scope, receive, send)


def wrap_http_auth_app(app: ASGIApp, *, auth_mode: str) -> ASGIApp:
    if str(auth_mode or "").strip().lower() == "api-key":
        return ApiKeyHeaderToBearerMiddleware(app)
    return app


def build_http_security_components(host: str, port: int) -> tuple[AuthSettings | None, TokenVerifier | None, TransportSecuritySettings | None, str]:
    auth_mode = str(os.getenv("MCP_AUTH_MODE", "")).strip().lower()
    allowed_origins = _parse_csv(os.getenv("MCP_ALLOWED_ORIGINS"))
    allowed_hosts = _parse_csv(os.getenv("MCP_ALLOWED_HOSTS")) or _default_allowed_hosts(host, int(port))

    transport_security = None
    if _is_http_transport_requested():
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=_parse_bool(os.getenv("MCP_ENABLE_DNS_REBINDING_PROTECTION"), True),
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )

    if auth_mode not in {"bearer", "api-key"}:
        return None, None, transport_security, auth_mode

    base_url = _derive_public_base_url(host, int(port))
    required_scopes = _parse_csv(os.getenv("MCP_AUTH_REQUIRED_SCOPES"))
    auth_settings = AuthSettings(
        issuer_url=str(os.getenv("MCP_AUTH_ISSUER_URL") or base_url).strip(),
        resource_server_url=str(os.getenv("MCP_RESOURCE_SERVER_URL") or base_url).strip(),
        required_scopes=required_scopes or None,
    )

    tokens = _load_static_token_records(auth_mode, base_url, required_scopes)
    if _is_http_transport_requested() and not tokens:
        raise RuntimeError(
            "HTTP auth is enabled but no static tokens are configured. Set MCP_AUTH_TOKEN, "
            "MCP_AUTH_TOKENS_JSON, or MCP_API_KEY/MCP_API_KEYS_JSON."
        )

    token_verifier: TokenVerifier | None = StaticTokenVerifier(tokens) if tokens else None
    return auth_settings, token_verifier, transport_security, auth_mode
