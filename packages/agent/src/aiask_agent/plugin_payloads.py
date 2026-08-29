from __future__ import annotations

from typing import Any

from .session_store_utils import redact_audit_text, sanitize_for_audit


def plugin_tools(plugin: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in list(plugin.get("tools") or []) if isinstance(item, dict) and item.get("name")]


def public_plugin_test_payload(
    *,
    object_name: str,
    success: bool,
    data: Any,
    error: Any = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    payload = {
        "object": object_name,
        "success": bool(success),
        "data": sanitize_for_audit(data, max_depth=6, max_items=120, max_text=2000),
        "error": redact_audit_text(error) if error is not None else None,
        "secrets_redacted": True,
    }
    if error_code:
        payload["error_code"] = error_code
    return payload


def plugin_self_test_payload(plugin: dict[str, Any], name: str) -> dict[str, Any]:
    tools = plugin_tools(plugin)
    commands = [dict(item) for item in list(plugin.get("commands") or []) if isinstance(item, dict) and item.get("name")]
    return {
        "object": "plugin.tool_test",
        "success": True,
        "data": {
            "plugin": str(plugin.get("name") or name),
            "test_type": "manifest",
            "enabled": bool(plugin.get("enabled")),
            "manifest_valid": True,
            "tools_count": len(tools),
            "commands_count": len(commands),
            "hooks_count": len(list(plugin.get("hooks") or [])),
            "available_tools": [str(item.get("name") or "") for item in tools],
            "available_commands": [str(item.get("name") or "") for item in commands],
            "note": "plugin manifest is readable; no executable tool runner is required for this self-test",
        },
        "error": None,
        "secrets_redacted": True,
    }
