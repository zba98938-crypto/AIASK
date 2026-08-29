"""Trade-risk explicit-token guard for finance-mcp-servers.

Mirrors the akshare-mcp `manager_protocol` trade_risk policy: any tool
that places or cancels live orders must require an explicit broker_token
in the request, validated against an env-configured value. Without a
valid token the call is rejected with a unified envelope carrying
`side_effect.level=trade_risk` + `explicit_token_required=True`.

This is a defence-in-depth check above whatever upstream auth layer the
caller already passed; it ensures that even an authenticated MCP client
cannot place orders accidentally — the broker_token must be passed
intentionally per call.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from .redaction import redact_public_text


class TradeGuardError(RuntimeError):
    """Raised when a trade_risk tool is invoked without a valid broker token."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def live_tools_enabled(*, provider_env_var: str | None = None) -> bool:
    """Return whether live order/cancel tools are explicitly enabled.

    Broker tokens protect each call, but direct MCP discovery should still be
    fail-closed. Operators must opt in before live tools are advertised or
    accepted outside the higher-level Agent/ActionIntent flow.
    """

    return _truthy(os.getenv("AIASK_FINANCE_ENABLE_LIVE_TOOLS")) or (
        bool(provider_env_var) and _truthy(os.getenv(str(provider_env_var)))
    )


def require_live_tools_enabled(*, provider_env_var: str | None = None) -> None:
    """Reject live order/cancel calls unless the server was explicitly opened."""

    if live_tools_enabled(provider_env_var=provider_env_var):
        return
    hint = "AIASK_FINANCE_ENABLE_LIVE_TOOLS=1"
    if provider_env_var:
        hint = f"{hint} or {provider_env_var}=1"
    raise TradeGuardError(
        "live order/cancel tools are disabled by default for direct MCP access; "
        f"enable {hint} only behind ActionIntent/control-token policy"
    )


def require_broker_token(args: dict[str, Any], *, env_var: str) -> None:
    """Validate that ``args['broker_token']`` matches ``os.environ[env_var]``.

    Raises ``TradeGuardError`` with a descriptive message on:
    - the env var is unset / blank (live trading disabled at server level)
    - ``broker_token`` is missing from args
    - ``broker_token`` does not match the configured value
    """
    expected = str(os.getenv(env_var) or "").strip()
    if not expected:
        raise TradeGuardError(
            f"{env_var} is not configured on the server; "
            "live trading is disabled until ops sets the broker token"
        )
    provided = str((args or {}).get("broker_token") or "").strip()
    if not provided:
        raise TradeGuardError(
            "broker_token is required for order placement/cancellation; "
            "this is a trade_risk action and must be confirmed per call"
        )
    if not hmac.compare_digest(provided, expected):
        raise TradeGuardError("broker_token mismatch")


def trade_risk_side_effect() -> dict[str, Any]:
    """Return canonical side-effect metadata for live order/cancel paths."""
    return {
        "level": "trade_risk",
        "confirmation_required": True,
        "explicit_token_required": True,
        "idempotent": False,
    }


def trade_risk_envelope(error: TradeGuardError, *, tool: str) -> dict[str, Any]:
    """Build a unified rejection envelope for trade_risk gate failures."""
    return {
        "success": False,
        "data": None,
        "error": redact_public_text(error),
        "error_code": "TRADE_RISK_TOKEN_REQUIRED",
        "meta": {
            "tool": tool,
            "side_effect": trade_risk_side_effect(),
        },
    }
