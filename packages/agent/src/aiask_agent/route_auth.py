from __future__ import annotations

import hmac
import ipaddress
import os
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request


def extract_bearer_token(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except Exception:
        return value in {"localhost", "testclient"}


def hermes_full_enabled() -> bool:
    return str(os.getenv("AIASK_AGENT_ENABLE_HERMES_FULL", "")).strip().lower() in {"1", "true", "yes", "on"}


def mode_error_status(reason: str | None) -> int:
    return 503 if reason in {"control token is not configured", "AIASK native Hermes full mode is not enabled"} else 401


def control_token_configured() -> bool:
    return bool(
        str(os.getenv("AIASK_AGENT_CONTROL_TOKEN", "")).strip()
        or str(os.getenv("AIASK_LOCAL_CONTROL_TOKEN", "")).strip()
    )


def token_matches(provided: str | None, expected: str | None) -> bool:
    provided_text = str(provided or "")
    expected_text = str(expected or "")
    return bool(provided_text and expected_text and hmac.compare_digest(provided_text, expected_text))


class RouteAuthorizer:
    def __init__(
        self,
        *,
        runtime: Any,
        build_full_runtime: Callable[[], Any],
        request_user_id_from_payload: Callable[..., str],
    ) -> None:
        self._runtime = runtime
        self._build_full_runtime = build_full_runtime
        self._request_user_id_from_payload = request_user_id_from_payload

    def api_authorized(self, request: Request) -> bool:
        client_host = request.client.host if request.client else "127.0.0.1"
        if is_loopback(client_host):
            return True
        expected = str(os.getenv("AIASK_AGENT_API_TOKEN", "")).strip()
        return token_matches(extract_bearer_token(request.headers.get("authorization")), expected)

    def control_authorized(self, request: Request) -> tuple[bool, str | None]:
        client_host = request.client.host if request.client else "127.0.0.1"
        if not is_loopback(client_host):
            return False, "control endpoint is loopback only"
        expected = (
            str(os.getenv("AIASK_AGENT_CONTROL_TOKEN", "")).strip()
            or str(os.getenv("AIASK_LOCAL_CONTROL_TOKEN", "")).strip()
        )
        if not expected:
            return False, "control token is not configured"
        token = (
            extract_bearer_token(request.headers.get("x-aiask-agent-control-token"))
            or extract_bearer_token(request.headers.get("x-aiask-local-control-token"))
            or extract_bearer_token(request.headers.get("authorization"))
        )
        if not token_matches(token, expected):
            return False, "invalid control token"
        return True, None

    def full_authorized(self, request: Request) -> tuple[bool, str | None]:
        if not hermes_full_enabled():
            return False, "AIASK native Hermes full mode is not enabled"
        return self.control_authorized(request)

    def select_runtime(self, payload: dict[str, Any], request: Request) -> tuple[Any, str]:
        mode = str(payload.get("mode") or "finance_safe").strip() or "finance_safe"
        if mode == "finance_safe":
            return self._runtime, mode
        if mode == "hermes_full":
            ok, reason = self.full_authorized(request)
            if not ok:
                raise HTTPException(mode_error_status(reason), detail=reason or "unauthorized")
            return self._build_full_runtime(), mode
        raise HTTPException(400, detail=f"unsupported mode: {mode}")

    def require_api(self, request: Request) -> None:
        if not self.api_authorized(request):
            raise HTTPException(401, detail="unauthorized")

    def require_control(self, request: Request) -> None:
        ok, reason = self.control_authorized(request)
        if not ok:
            raise HTTPException(mode_error_status(reason), detail=reason or "unauthorized")

    def require_full(self, request: Request) -> Any:
        ok, reason = self.full_authorized(request)
        if not ok:
            raise HTTPException(mode_error_status(reason), detail=reason or "unauthorized")
        return self._build_full_runtime()

    def require_user_scope(self, request: Request, requested_user_id: str | None) -> str:
        current_user_id = self._request_user_id_from_payload({}, headers=request.headers)
        requested = str(requested_user_id or current_user_id or "local").strip() or "local"
        if requested != current_user_id:
            ok, reason = self.control_authorized(request)
            if not ok:
                raise HTTPException(mode_error_status(reason), detail=reason or "cross-user access requires control token")
        return requested
