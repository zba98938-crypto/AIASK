from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .action_intents import create_external_action_intent_payload
from ..plugin_payloads import public_plugin_test_payload
from ..plugin_runtime import _safe_slug
from ..session_store_utils import sanitize_public_response


FullToolCall = Callable[[Request, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _public_control_payload(value: Any) -> Any:
    return sanitize_public_response(value, max_depth=10, max_items=500, max_text=4000)


def _wrapped_plugin_name(plugin: dict[str, Any], fallback: str, raw_name: str, *, kind: str) -> str:
    plugin_name = _safe_slug(str(plugin.get("name") or fallback)).replace("-", "_")
    tool_name = _safe_slug(raw_name).replace("-", "_")
    if kind == "command":
        return f"agent_plugin_{plugin_name}_command_{tool_name}"
    return f"agent_plugin_{plugin_name}_{tool_name}"


def _plugin_name_collisions(manager: Any, wrapped_name: str) -> list[dict[str, Any]]:
    for item in list(manager.tool_name_collisions() or []):
        if str(item.get("name") or "") == wrapped_name:
            return [dict(definition) for definition in list(item.get("definitions") or [])]
    return []


def _plugin_tool_result_payload(*, object_name: str, result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result or {})
    return public_plugin_test_payload(
        object_name=object_name,
        success=payload.get("success") is not False,
        data=payload.get("data"),
        error=payload.get("error"),
        error_code=payload.get("error_code"),
    )


def create_plugins_skills_router(
    *,
    require_full: Callable[[Request], Any],
    full_tool_call: FullToolCall,
    intent_store: Any,
    session_store: Any,
    build_full_runtime: Callable[[], Any],
    plugin_manager_factory: Callable[[], Any],
    plugin_self_test_payload: Callable[[dict[str, Any], str], dict[str, Any]],
    plugin_tools: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/skills")
    async def skills(request: Request) -> dict[str, Any]:
        require_full(request)
        result = await build_full_runtime().tool_registry.call_tool("agent_skill_manage", {"action": "snapshot"})
        snapshot = dict(result.get("data") or {})
        rows = []
        for item in list(snapshot.get("skills") or []):
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("id", str(row.get("name") or ""))
            row.setdefault("type", "local")
            row.setdefault("status", "enabled" if row.get("state") == "active" else str(row.get("state") or "unknown"))
            row.setdefault("enabled", row.get("state") == "active")
            rows.append(row)
        return {
            "object": "list",
            "data": _public_control_payload({"skills": rows, "root": snapshot.get("root"), "count": len(rows)}),
            "meta": _public_control_payload({"root": snapshot.get("root"), "count": len(rows)}),
            "secrets_redacted": True,
        }

    @router.post("/v1/skills")
    async def skill_create(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = await request.json()
        data = dict(payload or {})
        name = str(data.get("name") or "").strip()
        if "content" not in data:
            description = str(data.get("description") or f"Runtime skill registered from {data.get('path') or 'Agent API'}.").strip()
            data["description"] = description
            data["content"] = f"---\ndescription: {description}\n---\n\n# {name or 'Runtime Skill'}\n"
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="skill.install",
            params={"action": "install", **data},
            request_payload=data,
            headers=request.headers,
            rationale=str(data.get("rationale") or "Skill installation writes durable runtime capability files and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "skill.install.intent"],
            object_name="skill.install_intent",
        )

    @router.patch("/v1/skills/{name}")
    async def skill_update(request: Request, name: str) -> dict[str, Any]:
        require_full(request)
        payload = await request.json()
        data = dict(payload or {})
        if "enabled" in data and "content" not in data:
            params = {"action": "set_enabled", "name": name, "enabled": bool(data.get("enabled", True))}
            return create_external_action_intent_payload(
                intent_store=intent_store,
                session_store=session_store,
                action="skill.set_enabled",
                params=params,
                request_payload={**data, "name": name},
                headers=request.headers,
                rationale=str(data.get("rationale") or "Skill enablement changes durable runtime capability state and requires ActionIntent confirmation."),
                source_chain=["aiask_agent.server", "skill.set_enabled.intent"],
                object_name="skill.set_enabled_intent",
            )
        if "content" not in data:
            description = str(data.get("description") or f"Runtime skill registered from {data.get('path') or 'Agent API'}.").strip()
            data["description"] = description
            data["content"] = f"---\ndescription: {description}\n---\n\n# {name}\n"
        params = {"action": "update", "name": name, **data}
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="skill.update",
            params=params,
            request_payload={**data, "name": name},
            headers=request.headers,
            rationale=str(data.get("rationale") or "Skill updates write durable runtime capability files and require ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "skill.update.intent"],
            object_name="skill.update_intent",
        )

    @router.delete("/v1/skills/{name}")
    async def skill_delete(request: Request, name: str) -> dict[str, Any]:
        require_full(request)
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="skill.uninstall",
            params={"action": "uninstall", "name": name},
            request_payload={"name": name},
            headers=request.headers,
            rationale="Skill removal changes durable runtime capability files and requires ActionIntent confirmation.",
            source_chain=["aiask_agent.server", "skill.uninstall.intent"],
            object_name="skill.uninstall_intent",
        )

    @router.get("/v1/plugins")
    async def plugins(request: Request) -> dict[str, Any]:
        require_full(request)
        return {
            "object": "list",
            "data": _public_control_payload(plugin_manager_factory().list()),
            "secrets_redacted": True,
        }

    @router.post("/v1/plugins")
    async def plugin_upsert(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = await request.json()
        data = dict(payload or {})
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="plugin.upsert",
            params={"action": "upsert", **data},
            request_payload=data,
            headers=request.headers,
            rationale=str(data.get("rationale") or "Plugin updates change durable runtime capability state and require ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "plugin.upsert.intent"],
            object_name="plugin.upsert_intent",
        )

    @router.patch("/v1/plugins/{name}")
    async def plugin_toggle(request: Request, name: str) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        action = "enable" if bool(payload.get("enabled", True)) else "disable"
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action=f"plugin.{action}",
            params={"action": action, "name": name, **payload},
            request_payload={**payload, "name": name},
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Plugin enablement changes durable runtime capability state and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", f"plugin.{action}.intent"],
            object_name=f"plugin.{action}_intent",
        )

    @router.post("/v1/plugins/{name}/tools/{tool}/test")
    async def plugin_tool_test(request: Request, name: str, tool: str) -> dict[str, Any]:
        require_full(request)
        manager = plugin_manager_factory()
        plugin = manager.get(name)
        if not plugin:
            raise HTTPException(404, detail=f"plugin not found: {name}")
        if str(tool or "").strip().lower() in {"", "__manifest__", "manifest", "self-test", "self_test"}:
            return plugin_self_test_payload(plugin, name)
        wrapped = _wrapped_plugin_name(plugin, name, tool, kind="tool")
        payload = dict(await request.json() or {})
        collisions = _plugin_name_collisions(manager, wrapped)
        if collisions:
            return public_plugin_test_payload(
                object_name="plugin.tool_test",
                success=False,
                data={
                    "plugin": str(plugin.get("name") or name),
                    "tool": tool,
                    "wrapped_name": wrapped,
                    "collisions": collisions,
                    "configured": False,
                },
                error=f"plugin tool wrapper name collision for {wrapped}",
                error_code="PLUGIN_TOOL_NAME_COLLISION",
            )
        full = build_full_runtime()
        if full.tool_registry.get(wrapped) is None:
            return public_plugin_test_payload(
                object_name="plugin.tool_test",
                success=False,
                data={
                    "plugin": str(plugin.get("name") or name),
                    "tool": tool,
                    "available_tools": [str(item.get("name") or "") for item in plugin_tools(plugin)],
                    "configured": False,
                },
                error=f"plugin tool is not configured: {wrapped}",
                error_code="PLUGIN_TOOL_NOT_CONFIGURED",
            )
        return _plugin_tool_result_payload(
            object_name="plugin.tool_test",
            result=await full.tool_registry.call_tool(wrapped, payload),
        )

    @router.get("/v1/plugins/{name}/commands")
    async def plugin_commands(request: Request, name: str) -> dict[str, Any]:
        require_full(request)
        manager = plugin_manager_factory()
        if not manager.get(name):
            raise HTTPException(404, detail=f"plugin not found: {name}")
        return {
            "object": "list",
            "data": _public_control_payload(manager.list_commands(name)),
            "secrets_redacted": True,
        }

    @router.post("/v1/plugins/{name}/commands/{command}/test")
    async def plugin_command_test(request: Request, name: str, command: str) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        manager = plugin_manager_factory()
        plugin = manager.get(name)
        if not plugin:
            return public_plugin_test_payload(
                object_name="plugin.command_test",
                success=False,
                data={"plugin": name, "command": command, "configured": False},
                error=f"plugin not found: {name}",
                error_code="PLUGIN_COMMAND_NOT_CONFIGURED",
            )
        wrapped = _wrapped_plugin_name(plugin, name, command, kind="command")
        collisions = _plugin_name_collisions(manager, wrapped)
        if collisions:
            return public_plugin_test_payload(
                object_name="plugin.command_test",
                success=False,
                data={
                    "plugin": name,
                    "command": command,
                    "wrapped_name": wrapped,
                    "collisions": collisions,
                    "configured": False,
                },
                error=f"plugin command wrapper name collision for {wrapped}",
                error_code="PLUGIN_COMMAND_NAME_COLLISION",
            )
        full = build_full_runtime()
        if full.tool_registry.get(wrapped) is None:
            return public_plugin_test_payload(
                object_name="plugin.command_test",
                success=False,
                data={"plugin": name, "command": command, "configured": False},
                error=f"plugin command is not configured: {wrapped}",
                error_code="PLUGIN_COMMAND_NOT_CONFIGURED",
            )
        return _plugin_tool_result_payload(
            object_name="plugin.command_test",
            result=await full.tool_registry.call_tool(wrapped, payload),
        )

    return router
