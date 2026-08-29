from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .approvals import ApprovalStore, public_approval


ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
BLOCKED_DOMAINS = {
    "shell_command",
    "command_line",
    "python_script",
    "pyscript",
    "hassio",
    "rest_command",
}


def configured() -> bool:
    return bool(os.getenv("HASS_URL") and os.getenv("HASS_TOKEN"))


def _base_url() -> str:
    return str(os.getenv("HASS_URL", "http://homeassistant.local:8123")).rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.getenv('HASS_TOKEN', '')}",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 15.0) -> Any:
    if not configured():
        return {"configured": False, "required_env": ["HASS_URL", "HASS_TOKEN"]}
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{_base_url()}{path}", data=body, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Home Assistant HTTP {exc.code}: {exc.reason}") from exc


def list_entities(*, domain: str | None = None, area: str | None = None) -> dict[str, Any]:
    data = _request("GET", "/api/states")
    if isinstance(data, dict) and data.get("configured") is False:
        return data
    states = data if isinstance(data, list) else []
    if domain:
        states = [item for item in states if str(item.get("entity_id") or "").startswith(f"{domain}.")]
    if area:
        area_lower = area.lower()
        states = [
            item
            for item in states
            if area_lower in str((item.get("attributes") or {}).get("friendly_name") or "").lower()
            or area_lower in str((item.get("attributes") or {}).get("area") or "").lower()
        ]
    entities = [
        {
            "entity_id": item.get("entity_id"),
            "state": item.get("state"),
            "friendly_name": (item.get("attributes") or {}).get("friendly_name", ""),
        }
        for item in states
    ]
    return {"configured": True, "count": len(entities), "entities": entities}


def get_state(entity_id: str) -> dict[str, Any]:
    if not ENTITY_ID_RE.match(str(entity_id or "")):
        raise ValueError("invalid Home Assistant entity_id")
    data = _request("GET", f"/api/states/{entity_id}", timeout=10)
    if isinstance(data, dict) and data.get("configured") is False:
        return data
    return {
        "configured": True,
        "entity_id": data.get("entity_id"),
        "state": data.get("state"),
        "attributes": data.get("attributes") or {},
        "last_changed": data.get("last_changed"),
        "last_updated": data.get("last_updated"),
    }


def list_services() -> dict[str, Any]:
    data = _request("GET", "/api/services")
    if isinstance(data, dict) and data.get("configured") is False:
        return data
    return {"configured": True, "services": data if isinstance(data, list) else []}


def list_events() -> dict[str, Any]:
    data = _request("GET", "/api/events")
    if isinstance(data, dict) and data.get("configured") is False:
        return data
    return {"configured": True, "events": data if isinstance(data, list) else []}


def list_registry(kind: str) -> dict[str, Any]:
    token = str(kind or "").strip().lower()
    if token not in {"area", "device", "entity"}:
        raise ValueError("registry kind must be area, device, or entity")
    # Home Assistant exposes these registry endpoints in modern Core builds;
    # older builds may return 404, which is surfaced as a structured tool error.
    data = _request("GET", f"/api/config/{token}_registry")
    if isinstance(data, dict) and data.get("configured") is False:
        return data
    return {"configured": True, "kind": token, "items": data if isinstance(data, list) else []}


def normalize_service_call(
    *,
    domain: str,
    service: str,
    entity_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    domain = str(domain or "").strip().lower()
    service = str(service or "").strip().lower()
    if not SERVICE_NAME_RE.match(domain) or not SERVICE_NAME_RE.match(service):
        raise ValueError("invalid Home Assistant domain or service")
    if domain in BLOCKED_DOMAINS:
        raise PermissionError(f"blocked Home Assistant service domain: {domain}")
    if entity_id and not ENTITY_ID_RE.match(entity_id):
        raise ValueError("invalid Home Assistant entity_id")
    return {
        "domain": domain,
        "service": service,
        "entity_id": entity_id,
        "data": dict(data or {}),
    }


def call_service(
    *,
    domain: str,
    service: str,
    entity_id: str | None = None,
    data: dict[str, Any] | None = None,
    approval_id: str | None = None,
    state_path: Path | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    normalized = normalize_service_call(domain=domain, service=service, entity_id=entity_id, data=data)
    domain = str(normalized["domain"])
    service = str(normalized["service"])
    entity_id = normalized.get("entity_id")
    data = dict(normalized.get("data") or {})
    approvals = ApprovalStore(state_path)
    approval = approvals.get(str(approval_id or "")) if approval_id else None
    if not confirmed and (not approval or approval.get("status") != "approved"):
        pending = approvals.create(
            tool_name="agent_ha_call_service",
            action="homeassistant_call_service",
            arguments={"domain": domain, "service": service, "entity_id": entity_id, "data": dict(data or {})},
            reason="Home Assistant service calls can change physical device state",
        )
        return {"approval_required": True, "approval": public_approval(pending)}
    payload = dict(data or {})
    if entity_id:
        payload["entity_id"] = entity_id
    result = _request("POST", f"/api/services/{domain}/{service}", payload)
    return {"configured": configured(), "service": f"{domain}.{service}", "result": result}
