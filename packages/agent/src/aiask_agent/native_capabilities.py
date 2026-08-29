from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from . import homeassistant as ha_native
from .acp import ACPManager
from .control_context import control_context_active
from .gateway import DeliveryRouter, GatewayChannelDirectoryStore, GatewayConfigStore, GatewayMessageStore, GatewayRuntime
from .intents import ActionIntentStore, public_intent
from .mcp_client import MCPAggregator, MCPOAuthRequired
from .memory_providers import MemoryProviderManager
from .model_providers import ModelProviderRegistry, ProviderUsageStore
from .native_media import media_provider_catalog
from .native_media_handlers import build_media_handlers
from .native_message_outbox import MessageOutbox
from .native_planning_handlers import build_planning_handlers
from .native_skill_store import SkillStore
from .native_utils import _safe_slug
from .native_web_handlers import build_web_handlers
from .native_web_utils import _fetch_url, _json_request
from .numeric import bounded_int
from .platform_apis import DiscordServerClient, FeishuClient
from .plugin_runtime import NativePluginManager
from .rl_atropos import RLAtroposManager
from .security import SecurityScanner
from .session_store import AgentSessionStore, now_iso
from .skill_packs import SkillPackManager
from .terminal_backends import list_backends, sessions as terminal_backend_sessions
from .todo import FinancialTodoStore
from .tools.policy import ToolPolicy
from .tui import status as tui_status_payload
from .webhooks import WebhookStore


def _envelope(
    success: bool,
    *,
    data: Any = None,
    error: str | None = None,
    tool_name: str,
    level: str = "read_only",
    target: str | None = None,
    idempotent: bool = True,
) -> dict[str, Any]:
    return {
        "success": bool(success),
        "data": data,
        "error": error,
        "meta": {
            "trace_id": f"aiask-agent:{tool_name}:{int(time.time() * 1000)}:{uuid4().hex[:8]}",
            "source_chain": ["aiask_agent.native_capabilities"],
            "side_effect": {
                "level": level,
                "target": target or tool_name,
                "confirmation_required": False,
                "idempotent": idempotent,
            },
        },
    }


def build_native_capability_handlers(
    *,
    policy: ToolPolicy,
    session_store: AgentSessionStore,
    intent_store: ActionIntentStore | None = None,
    todo_store: FinancialTodoStore | None = None,
    skill_store: SkillStore | None = None,
    plugin_store: NativePluginManager | None = None,
    outbox: MessageOutbox | None = None,
) -> dict[str, Any]:
    intents = intent_store or ActionIntentStore()
    todos = todo_store or FinancialTodoStore(session_store.path)
    skills = skill_store or SkillStore()
    plugins = plugin_store or NativePluginManager()
    messages = outbox or MessageOutbox(session_store.path)
    mcp = MCPAggregator()
    model_registry = ModelProviderRegistry(usage_store=ProviderUsageStore(session_store.path))
    memory_providers = MemoryProviderManager(path=session_store.path)
    acp = ACPManager(mcp=mcp)
    security_scanner = SecurityScanner(policy=policy)
    skill_packs = SkillPackManager(skill_store=skills)
    webhooks = WebhookStore(session_store.path)
    directory = GatewayChannelDirectoryStore(session_store.path)
    gateway = GatewayRuntime(
        config=GatewayConfigStore(),
        messages=GatewayMessageStore(session_store.path),
        directory=directory,
    )
    delivery = DeliveryRouter(config=gateway.config, messages=gateway.messages, directory=directory)
    rl = RLAtroposManager(session_store.path)
    from .learning_loop import LearningLoop

    learning = LearningLoop(session_store=session_store, state_path=session_store.path)
    web_handlers = build_web_handlers(_envelope)
    media_handlers = build_media_handlers(_envelope)
    planning_handlers = build_planning_handlers(_envelope, todos=todos, session_store=session_store)

    def _action_intent_required_envelope(
        *,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        rationale: str,
        error: str,
        level: str,
        target: str | None = None,
        ttl_seconds: Any = None,
    ) -> dict[str, Any]:
        intent = intents.create(
            action=action,
            params=dict(params or {}),
            user_id=params.get("user_id"),
            rationale=rationale,
            ttl_seconds=bounded_int(ttl_seconds, default=86400, minimum=60, maximum=604800),
        )
        intent_id = str(intent.get("intent_id") or "")
        payload = _envelope(
            False,
            data={
                "required_tool": "agent_action_intent_get",
                "required_confirmation_endpoint": f"/intents/{intent_id}/confirm",
                "intent": public_intent(intent),
                "action_intent_required": True,
                "executed": False,
            },
            error=error,
            tool_name=tool_name,
            level=level,
            target=target or action,
            idempotent=False,
        )
        payload["error_code"] = "ACTION_INTENT_REQUIRED"
        payload["meta"]["side_effect"]["confirmation_required"] = True
        payload["meta"]["action_intent_required"] = True
        return payload

    def _external_action_intent_envelope(
        *,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        rationale: str,
        target: str | None = None,
        ttl_seconds: Any = None,
    ) -> dict[str, Any]:
        return _action_intent_required_envelope(
            tool_name=tool_name,
            action=action,
            params=params,
            rationale=rationale,
            error="external message delivery requires ActionIntent confirmation",
            level="external_message",
            target=target,
            ttl_seconds=ttl_seconds,
        )

    def _capability_action_intent_envelope(
        *,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        level: str,
        target: str | None = None,
        ttl_seconds: Any = None,
    ) -> dict[str, Any]:
        return _action_intent_required_envelope(
            tool_name=tool_name,
            action=action,
            params=params,
            rationale=str(params.get("rationale") or f"{tool_name} {action} requires ActionIntent confirmation."),
            error="capability management requires ActionIntent confirmation",
            level=level,
            target=target or action,
            ttl_seconds=ttl_seconds,
        )

    def _stateful_action_intent_envelope(
        *,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        level: str,
        target: str | None = None,
        rationale: str | None = None,
        error: str | None = None,
        ttl_seconds: Any = None,
    ) -> dict[str, Any]:
        return _action_intent_required_envelope(
            tool_name=tool_name,
            action=action,
            params={**dict(params or {}), "_state_path": str(session_store.path)},
            rationale=str(rationale or params.get("rationale") or f"{tool_name} {action} requires ActionIntent confirmation."),
            error=str(error or "stateful tool action requires ActionIntent confirmation"),
            level=level,
            target=target or action,
            ttl_seconds=ttl_seconds,
        )

    def _control_context_required_envelope(
        *,
        tool_name: str,
        action: str,
        level: str,
        target: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        message = reason or f"{tool_name} {action} requires a local control-token route."
        payload = _envelope(
            False,
            data={
                "control_context_required": True,
                "action_intent_required": False,
                "executed": False,
                "action": action,
                "reason": message,
            },
            error=message,
            tool_name=tool_name,
            level=level,
            target=target or action,
            idempotent=False,
        )
        payload["error_code"] = "CONTROL_CONTEXT_REQUIRED"
        payload["meta"]["side_effect"]["confirmation_required"] = True
        payload["meta"]["control_context_required"] = True
        return payload

    def _gateway_delivery_params(arguments: dict[str, Any], *, default_platform: str = "local") -> dict[str, Any]:
        return {
            "platform": str(arguments.get("platform") or default_platform),
            "target": str(arguments.get("target") or ""),
            "message": str(arguments.get("message") or ""),
            "thread_id": arguments.get("thread_id"),
            "session_id": arguments.get("session_id"),
            "user_id": arguments.get("user_id"),
            "media_paths": [str(item) for item in list(arguments.get("media_paths") or [])],
        }

    async def skill_list(_: dict[str, Any]) -> dict[str, Any]:
        return _envelope(True, data={"skills": skills.list()}, tool_name="agent_skill_list")

    async def skill_view(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return _envelope(
                True,
                data={
                    "skill": skills.view(
                        str(arguments.get("name") or ""),
                        max_chars=bounded_int(arguments.get("max_chars"), default=50000, minimum=1, maximum=200000),
                    )
                },
                tool_name="agent_skill_view",
                level="read_only",
                target=str(arguments.get("name") or ""),
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_skill_view", level="read_only")

    async def skill_save(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not control_context_active():
                return _capability_action_intent_envelope(
                    tool_name="agent_skill_save",
                    action="skill.save",
                    params=dict(arguments or {}),
                    level="filesystem_write",
                    target=str(arguments.get("name") or "skill"),
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            item = skills.save(
                str(arguments.get("name") or ""),
                str(arguments.get("content") or ""),
                description=arguments.get("description"),
            )
            return _envelope(
                True,
                data={"skill": item},
                tool_name="agent_skill_save",
                level="filesystem_write",
                target=item["path"],
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_skill_save", level="filesystem_write", idempotent=False)

    async def skill_manage(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_skill_manage"
        action = str(arguments.get("action") or "search").strip().lower()
        try:
            mutating_actions = {
                "install",
                "update",
                "uninstall",
                "pin",
                "unpin",
                "set_enabled",
                "archive",
                "restore",
                "rollback",
                "install_finance_templates",
            }
            if action in mutating_actions or (action == "snapshot" and bool(arguments.get("create_backup"))):
                level = "stateful" if action in {"pin", "unpin", "set_enabled"} else "filesystem_write"
                if not control_context_active():
                    return _capability_action_intent_envelope(
                        tool_name=tool,
                        action=f"skill.{action}",
                        params=dict(arguments or {}),
                        level=level,
                        target=str(arguments.get("name") or arguments.get("backup_id") or "skills"),
                        ttl_seconds=arguments.get("ttl_seconds"),
                    )
            if action == "search":
                query = str(arguments.get("query") or "").lower()
                data = {"skills": [item for item in skills.list() if not query or query in json.dumps(item, ensure_ascii=False).lower()]}
                level = "read_only"
            elif action in {"install", "update"}:
                content = str(arguments.get("content") or "").strip()
                if not content:
                    name = str(arguments.get("name") or "skill").strip() or "skill"
                    description = str(arguments.get("description") or f"Runtime skill registered from {arguments.get('path') or 'Agent API'}.").strip()
                    content = f"---\ndescription: {description}\n---\n\n# {name}\n"
                item = skills.save(
                    str(arguments.get("name") or ""),
                    content,
                    description=arguments.get("description"),
                )
                data = {"skill": item}
                level = "filesystem_write"
            elif action == "uninstall":
                name = _safe_slug(str(arguments.get("name") or ""))
                path = skills.root / name
                archived = skills.archive(name, reason="uninstall") if path.exists() else None
                data = {"name": name, "deleted": bool(path.exists() or archived), "archived": archived}
                level = "filesystem_write"
            elif action == "audit":
                data = skills.audit(dry_run=bool(arguments.get("dry_run", True)))
                level = "read_only"
            elif action == "snapshot":
                data = {"skills": skills.list(), "root": str(skills.root)}
                if bool(arguments.get("create_backup")):
                    data["backup"] = skills.backup(reason=str(arguments.get("reason") or "snapshot"))
                level = "read_only"
            elif action == "pin":
                data = {"skill": skills.pin(str(arguments.get("name") or ""), True)}
                level = "stateful"
            elif action == "unpin":
                data = {"skill": skills.pin(str(arguments.get("name") or ""), False)}
                level = "stateful"
            elif action == "set_enabled":
                data = {"skill": skills.set_enabled(str(arguments.get("name") or ""), bool(arguments.get("enabled", True)))}
                level = "stateful"
            elif action == "archive":
                data = {"skill": skills.archive(str(arguments.get("name") or ""), reason=arguments.get("reason"))}
                level = "filesystem_write"
            elif action == "restore":
                data = {"skill": skills.restore(str(arguments.get("name") or ""))}
                level = "filesystem_write"
            elif action == "rollback":
                data = {"rollback": skills.rollback(str(arguments.get("backup_id") or "").strip() or None)}
                level = "filesystem_write"
            elif action == "install_finance_templates":
                data = skills.install_finance_templates(overwrite=bool(arguments.get("overwrite")))
                level = "filesystem_write"
            else:
                raise ValueError(f"unsupported skill action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=str(arguments.get("name") or "skills"), idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="filesystem_write", idempotent=False)

    async def plugin_list(_: dict[str, Any]) -> dict[str, Any]:
        return _envelope(True, data={"plugins": plugins.list()}, tool_name="agent_plugin_list")

    async def plugin_set_enabled(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not control_context_active():
                return _capability_action_intent_envelope(
                    tool_name="agent_plugin_set_enabled",
                    action="plugin.set_enabled",
                    params=dict(arguments or {}),
                    level="stateful",
                    target=str(arguments.get("name") or "plugin"),
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            item = plugins.set_enabled(
                str(arguments.get("name") or ""),
                bool(arguments.get("enabled", True)),
                description=arguments.get("description"),
            )
            return _envelope(
                True,
                data={"plugin": item},
                tool_name="agent_plugin_set_enabled",
                level="stateful",
                target=item["name"],
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_plugin_set_enabled", level="stateful", idempotent=False)

    async def plugin_manage(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_plugin_manage"
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            if action in {"enable", "disable", "upsert"} and not control_context_active():
                return _capability_action_intent_envelope(
                    tool_name=tool,
                    action=f"plugin.{action}",
                    params=dict(arguments or {}),
                    level="stateful",
                    target=str(arguments.get("name") or "plugins"),
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            if action == "list":
                data = {"plugins": plugins.list()}
                level = "read_only"
            elif action == "inspect":
                data = {"plugin": plugins.get(str(arguments.get("name") or ""))}
                level = "read_only"
            elif action in {"enable", "disable"}:
                data = {"plugin": plugins.set_enabled(str(arguments.get("name") or ""), action == "enable", description=arguments.get("description"))}
                level = "stateful"
            elif action == "upsert":
                data = {"plugin": plugins.update(str(arguments.get("name") or ""), manifest=dict(arguments.get("manifest") or {}))}
                level = "stateful"
            else:
                raise ValueError(f"unsupported plugin action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=str(arguments.get("name") or "plugins"), idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def mcp_manage(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_mcp_manage"
        action = str(arguments.get("action") or "servers").strip().lower()
        try:
            if action == "servers":
                data = {"servers": mcp.servers_summary(include_all=True)}
            elif action == "tools":
                data = {"tools": mcp.tools_summary(include_all=True)}
            elif action in {"resources", "prompts"}:
                summary = mcp.resources_summary(include_all=True) if action == "resources" else mcp.prompts_summary(include_all=True)
                data = {action: summary, "enabled": bool(summary)}
            elif action == "oauth_status":
                data = {"servers": mcp.oauth_status(include_all=True)}
            elif action == "discover":
                data = await mcp.discover(str(arguments.get("server") or ""))
            elif action == "resource_read":
                data = await mcp.read_resource(str(arguments.get("server") or ""), str(arguments.get("uri") or ""))
            elif action == "prompt_get":
                data = await mcp.get_prompt(
                    str(arguments.get("server") or ""),
                    str(arguments.get("prompt") or arguments.get("name") or ""),
                    dict(arguments.get("arguments") or {}),
                )
            elif action == "oauth_start":
                data = mcp.oauth_start(
                    str(arguments.get("server") or ""),
                    redirect_uri=arguments.get("redirect_uri"),
                    scope=arguments.get("scope"),
                )
            elif action == "oauth_callback":
                if not control_context_active():
                    return _control_context_required_envelope(
                        tool_name=tool,
                        action="mcp.oauth_callback",
                        level="stateful",
                        target=str(arguments.get("server") or "mcp_oauth"),
                        reason="MCP OAuth callback can carry credentials and requires a local control-token route.",
                    )
                data = mcp.oauth_callback(str(arguments.get("server") or ""), dict(arguments.get("token") or arguments))
            elif action == "test":
                server_name = str(arguments.get("server") or "")
                data = {"server": server_name, "configured": any(item.get("name") == server_name for item in mcp.servers_summary(include_all=True))}
            else:
                raise ValueError(f"unsupported mcp action: {action}")
            return _envelope(True, data=data, tool_name=tool, level="read_only")
        except MCPOAuthRequired as exc:
            return _envelope(False, data=exc.payload, error="MCP OAuth authorization is required", tool_name=tool, level="stateful")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def model_manage(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_model_manage"
        action = str(arguments.get("action") or "status").strip().lower()
        try:
            if action == "status":
                data = model_registry.status()
                level = "read_only"
            elif action == "providers":
                usage = model_registry.usage_store.summary()
                data = {"providers": [item.public(usage) for item in model_registry.providers()]}
                level = "read_only"
            elif action == "credential_pool":
                data = model_registry.credential_pool_status(arguments.get("provider"))
                level = "read_only"
            elif action == "select":
                provider = str(arguments.get("provider") or model_registry.active_provider_name())
                selected = model_registry.select_credential(provider)
                data = {"provider": provider, "credential": selected.public() if selected else None, "selected": bool(selected)}
                level = "read_only"
            elif action == "record_attempt":
                if not control_context_active():
                    return _control_context_required_envelope(
                        tool_name=tool,
                        action="model.record_attempt",
                        level="stateful",
                        target=str(arguments.get("provider") or model_registry.active_provider_name()),
                        reason="Model provider attempt accounting requires an internal runtime control context.",
                    )
                data = model_registry.record_attempt(
                    provider=str(arguments.get("provider") or model_registry.active_provider_name()),
                    credential_id=str(arguments.get("credential_id") or ""),
                    success=bool(arguments.get("success")),
                    error=arguments.get("error"),
                )
                level = "stateful"
            elif action == "classify_error":
                data = {"error_class": model_registry.classify_error(arguments.get("error"))}
                level = "read_only"
            elif action == "prompt_cache":
                data = model_registry.status().get("prompt_cache") or {}
                level = "read_only"
            else:
                raise ValueError(f"unsupported model action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=action, idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def memory_manage(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_memory_manage"
        action = str(arguments.get("action") or "status").strip().lower()
        try:
            if action == "status":
                data = memory_providers.status()
                level = "read_only"
            elif action == "catalog":
                data = memory_providers.catalog()
                level = "read_only"
            elif action == "save":
                if not control_context_active():
                    return _stateful_action_intent_envelope(
                        tool_name=tool,
                        action="memory.save",
                        params=dict(arguments or {}),
                        level="stateful",
                        target=str(arguments.get("symbol") or arguments.get("research_topic") or "memory"),
                        rationale=str(arguments.get("rationale") or "Long-term memory writes require ActionIntent confirmation."),
                        error="memory mutation requires ActionIntent confirmation",
                        ttl_seconds=arguments.get("ttl_seconds"),
                    )
                data = {"memory": memory_providers.save(arguments)}
                level = "stateful"
            elif action == "search":
                data = {"memories": memory_providers.search(arguments)}
                level = "read_only"
            elif action == "audit":
                data = memory_providers.audit()
                level = "read_only"
            else:
                raise ValueError(f"unsupported memory action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=action, idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def acp_manage(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_acp_manage"
        action = str(arguments.get("action") or "status").strip().lower()
        try:
            if action in {"register_mcp_server", "remove_mcp_server"} and not control_context_active():
                return _capability_action_intent_envelope(
                    tool_name=tool,
                    action=f"acp.{action}",
                    params=dict(arguments or {}),
                    level="stateful",
                    target=str(arguments.get("name") or action),
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            if action == "status":
                data = acp.status()
                level = "read_only"
            elif action == "readiness":
                data = acp.readiness()
                level = "read_only"
            elif action == "register_mcp_server":
                data = acp.register_server(arguments)
                level = "stateful"
            elif action == "remove_mcp_server":
                data = acp.remove_server(str(arguments.get("name") or ""))
                level = "stateful"
            else:
                raise ValueError(f"unsupported ACP action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=str(arguments.get("name") or action), idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def security_scan(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_security_scan"
        try:
            return _envelope(
                True,
                data=security_scanner.scan(arguments),
                tool_name=tool,
                level="read_only",
                target=str(arguments.get("path") or arguments.get("url") or "inline"),
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def skill_pack_manage(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_skill_pack_manage"
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            if action == "install" and not control_context_active():
                return _capability_action_intent_envelope(
                    tool_name=tool,
                    action="skill_pack.install",
                    params=dict(arguments or {}),
                    level="filesystem_write",
                    target=str(arguments.get("pack") or arguments.get("name") or "skill_pack"),
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            if action == "list":
                data = {"packs": skill_packs.list()}
                level = "read_only"
            elif action == "status":
                data = skill_packs.status()
                level = "read_only"
            elif action == "install":
                pack = str(arguments.get("pack") or arguments.get("name") or "")
                data = skill_packs.install(pack, overwrite=bool(arguments.get("overwrite")))
                level = "filesystem_write"
            elif action == "audit":
                data = skill_packs.audit()
                level = "read_only"
            else:
                raise ValueError(f"unsupported skill pack action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=str(arguments.get("pack") or action), idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="filesystem_write", idempotent=False)

    async def terminal_backends(arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            if action == "sessions":
                data = {
                    "sessions": terminal_backend_sessions(
                        state_path=session_store.path,
                        limit=bounded_int(arguments.get("limit"), default=200, minimum=1, maximum=1000),
                    )
                }
            elif action == "list":
                data = {"backends": list_backends()}
            else:
                raise ValueError(f"unsupported terminal backend action: {action}")
            return _envelope(True, data=data, tool_name="agent_terminal_backends", level="read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_terminal_backends", level="read_only")

    async def tui_status(_: dict[str, Any]) -> dict[str, Any]:
        return _envelope(True, data=tui_status_payload(), tool_name="agent_tui_status", level="read_only")

    async def gateway_status(_: dict[str, Any]) -> dict[str, Any]:
        return _envelope(True, data=gateway.status(), tool_name="agent_gateway_status", level="read_only")

    async def gateway_platforms(arguments: dict[str, Any]) -> dict[str, Any]:
        platform = str(arguments.get("platform") or "").strip()
        data = {"platforms": [gateway.config.platform_status(platform).to_dict()] if platform else gateway.list_platforms()}
        return _envelope(True, data=data, tool_name="agent_gateway_platforms", level="read_only")

    async def gateway_send_message(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_gateway_send_message"
        try:
            action = str(arguments.get("action") or "send").strip().lower()
            if action == "list":
                return _envelope(True, data={"platforms": gateway.list_platforms()}, tool_name=tool, level="read_only")
            if action != "send":
                raise ValueError(f"unsupported gateway message action: {action}")
            params = _gateway_delivery_params(arguments)
            return _external_action_intent_envelope(
                tool_name=tool,
                action="gateway.send_message",
                params=params,
                rationale=str(arguments.get("rationale") or "Gateway model tool send requires ActionIntent confirmation."),
                target=str(arguments.get("target") or ""),
                ttl_seconds=arguments.get("ttl_seconds"),
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="external_message", idempotent=False)

    async def gateway_history(arguments: dict[str, Any]) -> dict[str, Any]:
        return _envelope(
            True,
            data={
                "messages": gateway.messages.list(
                    platform=arguments.get("platform"),
                    limit=bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=1000),
                )
            },
            tool_name="agent_gateway_history",
            level="read_only",
        )

    async def gateway_pairing(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_gateway_pairing"
        action = str(arguments.get("action") or "status").strip().lower()
        payload = {
            "action": action,
            "platform": arguments.get("platform"),
            "user_id": arguments.get("user_id"),
            "session_id": arguments.get("session_id"),
            "configured": True,
        }
        if action == "status":
            return _envelope(True, data=payload, tool_name=tool, level="read_only", idempotent=True)
        if action != "create":
            return _envelope(False, error=f"unsupported gateway pairing action: {action}", tool_name=tool, level="stateful", idempotent=False)
        if not control_context_active():
            return _stateful_action_intent_envelope(
                tool_name=tool,
                action="gateway.pairing_create",
                params=payload,
                level="stateful",
                target=str(arguments.get("platform") or "gateway_pairing"),
                rationale=str(arguments.get("rationale") or "Gateway pairing changes platform/session routing state and requires ActionIntent confirmation."),
                error="Gateway pairing creation requires ActionIntent confirmation",
                ttl_seconds=arguments.get("ttl_seconds"),
            )
        return _envelope(True, data=payload, tool_name=tool, level="stateful", idempotent=False)

    async def gateway_directory(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_gateway_directory"
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            if action == "list":
                data = {
                    "items": directory.list(
                        platform=arguments.get("platform"),
                        kind=arguments.get("kind"),
                        limit=bounded_int(arguments.get("limit"), default=200, minimum=1, maximum=1000),
                    )
                }
                level = "read_only"
            elif action == "resolve":
                data = {
                    "item": directory.resolve(
                        platform=arguments.get("platform"),
                        name=str(arguments.get("name") or arguments.get("target") or ""),
                        kind=arguments.get("kind"),
                    )
                }
                level = "read_only"
            elif action == "refresh":
                if not control_context_active():
                    return _stateful_action_intent_envelope(
                        tool_name=tool,
                        action="gateway.directory_refresh",
                        params=dict(arguments or {}),
                        level="stateful",
                        target="gateway_directory",
                        rationale=str(arguments.get("rationale") or "Refreshing the gateway directory changes platform routing aliases and requires ActionIntent confirmation."),
                        error="Gateway directory refresh requires ActionIntent confirmation",
                        ttl_seconds=arguments.get("ttl_seconds"),
                    )
                data = directory.refresh(config=gateway.config)
                level = "stateful"
            elif action == "upsert":
                params = {
                    "platform": str(arguments.get("platform") or "local"),
                    "name": str(arguments.get("name") or ""),
                    "target": str(arguments.get("target") or ""),
                    "kind": str(arguments.get("kind") or "channel"),
                    "thread_id": arguments.get("thread_id"),
                    "metadata": dict(arguments.get("metadata") or {}),
                }
                if not control_context_active():
                    return _stateful_action_intent_envelope(
                        tool_name=tool,
                        action="gateway.directory_upsert",
                        params=params,
                        level="stateful",
                        target=str(arguments.get("name") or arguments.get("target") or "gateway_directory"),
                        rationale=str(arguments.get("rationale") or "Updating the gateway directory changes platform routing aliases and requires ActionIntent confirmation."),
                        error="Gateway directory update requires ActionIntent confirmation",
                        ttl_seconds=arguments.get("ttl_seconds"),
                    )
                data = {
                    "item": directory.upsert(
                        platform=params["platform"],
                        name=params["name"],
                        target=params["target"],
                        kind=params["kind"],
                        thread_id=params.get("thread_id"),
                        metadata=dict(params.get("metadata") or {}),
                    )
                }
                level = "stateful"
            else:
                raise ValueError(f"unsupported gateway directory action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def gateway_direct_deliver(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_gateway_direct_deliver"
        try:
            params = _gateway_delivery_params(arguments)
            return _external_action_intent_envelope(
                tool_name=tool,
                action="gateway.direct_deliver",
                params=params,
                rationale=str(arguments.get("rationale") or "Gateway direct delivery model tool requires ActionIntent confirmation."),
                target=str(arguments.get("target") or ""),
                ttl_seconds=arguments.get("ttl_seconds"),
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="external_message", idempotent=False)

    async def session_handoff(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_session_handoff"
        action = str(arguments.get("action") or "status").strip().lower()
        runtime_context = dict(arguments.get("_aiask_runtime_context") or {})

        def _scope_violation(message: str) -> dict[str, Any]:
            payload = _envelope(
                False,
                error=message,
                tool_name=tool,
                level="stateful",
                target=str(arguments.get("session_id") or arguments.get("handoff_id") or "session_handoff"),
                idempotent=False,
            )
            payload["error_code"] = "SESSION_HANDOFF_SCOPE_VIOLATION"
            payload["meta"]["session_handoff_guard"] = {
                "blocked": True,
                "reason": "runtime_context_scope_mismatch",
            }
            return payload

        try:
            if action == "request":
                if not runtime_context and not control_context_active():
                    return _control_context_required_envelope(
                        tool_name=tool,
                        action="session_handoff.request",
                        level="stateful",
                        target=str(arguments.get("session_id") or "session_handoff"),
                        reason="Session handoff requests outside the active runtime require a local control-token route.",
                    )
                runtime_session_id = str(runtime_context.get("session_id") or "").strip()
                requested_session_id = str(arguments.get("session_id") or runtime_session_id or "default").strip() or "default"
                if runtime_session_id and requested_session_id != runtime_session_id:
                    return _scope_violation("session handoff request cannot target a different session than the active runtime")
                runtime_user_id = str(runtime_context.get("user_id") or "").strip()
                requested_user_id = str(arguments.get("user_id") or runtime_user_id or "").strip()
                if runtime_user_id and requested_user_id and requested_user_id != runtime_user_id:
                    return _scope_violation("session handoff request cannot target a different user than the active runtime")
                metadata = dict(arguments.get("metadata") or {})
                if runtime_context:
                    metadata.update(
                        {
                            "handoff_kind": "ownership_transfer",
                            "source_session_id": runtime_context.get("session_id"),
                            "source_run_id": runtime_context.get("run_id"),
                            "source_trace_id": runtime_context.get("trace_id"),
                            "source_tool_call_id": runtime_context.get("parent_tool_call_id"),
                            "context_snapshot_id": runtime_context.get("context_snapshot_id"),
                        }
                    )
                item = session_store.request_handoff(
                    session_id=requested_session_id,
                    user_id=requested_user_id or None,
                    target=arguments.get("target"),
                    reason=arguments.get("reason"),
                    summary=arguments.get("summary"),
                    metadata=metadata,
                )
                session_store.set_session_handoff_state(
                    str(item.get("session_id") or "default"),
                    status="pending",
                    handoff_id=item.get("handoff_id"),
                    target=item.get("target"),
                    source_run_id=metadata.get("source_run_id"),
                    source_tool_call_id=metadata.get("source_tool_call_id"),
                    context_snapshot_id=metadata.get("context_snapshot_id"),
                    summary=item.get("summary"),
                    reason=item.get("reason"),
                    metadata=metadata,
                )
                item = session_store.get_handoff(str(item.get("handoff_id") or "")) or item
                data = {"handoff": item}
                level = "stateful"
            elif action == "status":
                handoff_id = str(arguments.get("handoff_id") or "").strip()
                if handoff_id:
                    data = {"handoff": session_store.get_handoff(handoff_id)}
                else:
                    items = session_store.list_handoffs(
                        session_id=arguments.get("session_id"),
                        limit=bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=1000),
                    )
                    data = {"handoffs": items, "latest": items[0] if items else None}
                level = "read_only"
            elif action == "list":
                data = {
                    "handoffs": session_store.list_handoffs(
                        session_id=arguments.get("session_id"),
                        limit=bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=1000),
                    )
                }
                level = "read_only"
            elif action in {"complete", "fail"}:
                if not runtime_context and not control_context_active():
                    return _control_context_required_envelope(
                        tool_name=tool,
                        action=f"session_handoff.{action}",
                        level="stateful",
                        target=str(arguments.get("handoff_id") or "session_handoff"),
                        reason="Session handoff lifecycle changes outside the active runtime require a local control-token route.",
                    )
                existing = session_store.get_handoff(str(arguments.get("handoff_id") or ""))
                runtime_session_id = str(runtime_context.get("session_id") or "").strip()
                if runtime_session_id and existing and str(existing.get("session_id") or "") != runtime_session_id:
                    return _scope_violation("session handoff lifecycle action cannot target a different session than the active runtime")
                status = "completed" if action == "complete" else "failed"
                handoff = session_store.update_handoff(
                    str(arguments.get("handoff_id") or ""),
                    status=status,
                    metadata=dict(arguments.get("metadata") or {}),
                )
                session_store.set_session_handoff_state(
                    str(handoff.get("session_id") or "default"),
                    status=status,
                    handoff_id=handoff.get("handoff_id"),
                    target=handoff.get("target"),
                    source_run_id=dict(handoff.get("metadata") or {}).get("source_run_id"),
                    source_tool_call_id=dict(handoff.get("metadata") or {}).get("source_tool_call_id"),
                    context_snapshot_id=dict(handoff.get("metadata") or {}).get("context_snapshot_id"),
                    summary=handoff.get("summary"),
                    reason=handoff.get("reason"),
                    metadata=dict(handoff.get("metadata") or {}),
                )
                data = {
                    "handoff": handoff,
                    "handoff_state": dict((session_store.get_session(str(handoff.get("session_id") or "")) or {}).get("metadata") or {}).get("handoff_state"),
                }
                level = "stateful"
            else:
                raise ValueError(f"unsupported session handoff action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=str(arguments.get("session_id") or arguments.get("handoff_id") or ""), idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def learning_status(_: dict[str, Any]) -> dict[str, Any]:
        return _envelope(True, data=learning.status(), tool_name="agent_learning_status", level="read_only")

    async def learning_review(arguments: dict[str, Any]) -> dict[str, Any]:
        return _envelope(
            True,
            data={
                "proposals": learning.review(
                    status=arguments.get("status"),
                    limit=bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=1000),
                )
            },
            tool_name="agent_learning_review",
            level="read_only",
        )

    async def learning_apply(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_learning_apply"
        try:
            if not control_context_active():
                return _stateful_action_intent_envelope(
                    tool_name=tool,
                    action="learning.apply",
                    params=dict(arguments or {}),
                    level="filesystem_write",
                    target=str(arguments.get("proposal_id") or "learning_proposal"),
                    rationale=str(arguments.get("rationale") or "Applying learning proposals can write skills or memory and requires ActionIntent confirmation."),
                    error="learning proposal application requires ActionIntent confirmation",
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            data = {"proposal": learning.apply(str(arguments.get("proposal_id") or ""))}
            return _envelope(True, data=data, tool_name=tool, level="filesystem_write", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="filesystem_write", idempotent=False)

    async def skill_reflect(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_skill_reflect"
        try:
            if not control_context_active():
                return _stateful_action_intent_envelope(
                    tool_name=tool,
                    action="learning.reflect_skill",
                    params=dict(arguments or {}),
                    level="stateful",
                    target=str(arguments.get("name") or "skill_reflection"),
                    rationale=str(arguments.get("rationale") or "Skill reflection creates a durable learning proposal and requires ActionIntent confirmation."),
                    error="skill reflection requires ActionIntent confirmation",
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            data = {"proposal": learning.reflect_skill(name=str(arguments.get("name") or ""), observation=str(arguments.get("observation") or ""))}
            return _envelope(True, data=data, tool_name=tool, level="stateful", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def ha_list_entities(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            data = ha_native.list_entities(domain=arguments.get("domain"), area=arguments.get("area"))
            return _envelope(bool(data.get("configured", True)), data=data, error=None if data.get("configured", True) else "Home Assistant is not configured", tool_name="agent_ha_list_entities")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_ha_list_entities")

    async def ha_get_state(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            data = ha_native.get_state(str(arguments.get("entity_id") or ""))
            return _envelope(bool(data.get("configured", True)), data=data, error=None if data.get("configured", True) else "Home Assistant is not configured", tool_name="agent_ha_get_state")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_ha_get_state")

    async def ha_list_services(_: dict[str, Any]) -> dict[str, Any]:
        try:
            data = ha_native.list_services()
            return _envelope(bool(data.get("configured", True)), data=data, error=None if data.get("configured", True) else "Home Assistant is not configured", tool_name="agent_ha_list_services")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_ha_list_services")

    async def ha_list_events(_: dict[str, Any]) -> dict[str, Any]:
        try:
            data = ha_native.list_events()
            return _envelope(bool(data.get("configured", True)), data=data, error=None if data.get("configured", True) else "Home Assistant is not configured", tool_name="agent_ha_list_events")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_ha_list_events")

    async def ha_list_registry(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            data = ha_native.list_registry(str(arguments.get("kind") or "entity"))
            return _envelope(bool(data.get("configured", True)), data=data, error=None if data.get("configured", True) else "Home Assistant is not configured", tool_name="agent_ha_list_registry")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_ha_list_registry")

    async def ha_call_service(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_ha_call_service"
        try:
            params = ha_native.normalize_service_call(
                domain=str(arguments.get("domain") or ""),
                service=str(arguments.get("service") or ""),
                entity_id=arguments.get("entity_id"),
                data=dict(arguments.get("data") or {}),
            )
            rationale = str(arguments.get("rationale") or "Home Assistant service calls can change physical device state.")
            return _action_intent_required_envelope(
                tool_name=tool,
                action="homeassistant.call_service",
                params={**params, "rationale": rationale},
                rationale=rationale,
                error="Home Assistant service calls require ActionIntent confirmation",
                level="physical_state_change",
                target=f"{params['domain']}.{params['service']}",
                ttl_seconds=arguments.get("ttl_seconds"),
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="physical_state_change", idempotent=False)

    async def feishu_doc_read(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            data = FeishuClient(domain=str(arguments.get("domain") or "feishu")).read_doc(
                document_id=arguments.get("document_id"),
                url=arguments.get("url"),
            )
            success = bool(data.get("configured")) and not data.get("error")
            return _envelope(success, data=data, error=None if success else str(data.get("error") or "Feishu credentials are not configured"), tool_name="agent_feishu_doc_read")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_feishu_doc_read")

    async def feishu_comments(arguments: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
        try:
            client = FeishuClient(domain=str(arguments.get("domain") or "feishu"))
            if tool_name == "agent_feishu_drive_list_comments":
                data = client.list_comments(
                    file_token=str(arguments.get("file_token") or ""),
                    file_type=str(arguments.get("file_type") or "docx"),
                    page_token=arguments.get("page_token"),
                    page_size=bounded_int(arguments.get("page_size"), default=50, minimum=1, maximum=100),
                )
                level = "read_only"
            elif tool_name == "agent_feishu_drive_list_comment_replies":
                data = client.list_comment_replies(
                    file_token=str(arguments.get("file_token") or ""),
                    comment_id=str(arguments.get("comment_id") or ""),
                    file_type=str(arguments.get("file_type") or "docx"),
                    page_token=arguments.get("page_token"),
                    page_size=bounded_int(arguments.get("page_size"), default=100, minimum=1, maximum=100),
                )
                level = "read_only"
            elif tool_name == "agent_feishu_drive_reply_comment":
                return _external_action_intent_envelope(
                    tool_name=tool_name,
                    action="feishu_drive.reply_comment",
                    params={
                        "domain": str(arguments.get("domain") or "feishu"),
                        "file_token": str(arguments.get("file_token") or ""),
                        "comment_id": str(arguments.get("comment_id") or ""),
                        "message": str(arguments.get("message") or ""),
                        "user_id": arguments.get("user_id"),
                    },
                    rationale=str(arguments.get("rationale") or "Feishu drive comment reply requires ActionIntent confirmation."),
                    target=str(arguments.get("file_token") or ""),
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            else:
                return _external_action_intent_envelope(
                    tool_name=tool_name,
                    action="feishu_drive.add_comment",
                    params={
                        "domain": str(arguments.get("domain") or "feishu"),
                        "file_token": str(arguments.get("file_token") or ""),
                        "message": str(arguments.get("message") or ""),
                        "user_id": arguments.get("user_id"),
                    },
                    rationale=str(arguments.get("rationale") or "Feishu drive comment creation requires ActionIntent confirmation."),
                    target=str(arguments.get("file_token") or ""),
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            response = dict(data.get("response") or {})
            success = bool(data.get("configured")) and (not response or bool(response.get("ok")))
            return _envelope(success, data=data, error=None if success else str(response.get("error") or "Feishu API call failed"), tool_name=tool_name, level=level, idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool_name, level="external_message", idempotent=False)

    async def discord_channel_send(arguments: dict[str, Any]) -> dict[str, Any]:
        return _external_action_intent_envelope(
            tool_name="agent_discord_channel_send",
            action="discord.channel_send",
            params={
                "channel_id": str(arguments.get("channel_id") or ""),
                "message": str(arguments.get("message") or ""),
                "thread_id": arguments.get("thread_id"),
                "session_id": arguments.get("session_id"),
                "user_id": arguments.get("user_id"),
            },
            rationale=str(arguments.get("rationale") or "Discord channel send requires ActionIntent confirmation."),
            target=str(arguments.get("channel_id") or ""),
            ttl_seconds=arguments.get("ttl_seconds"),
        )

    async def discord_server(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_discord_server"
        action = str(arguments.get("action") or "").strip()
        admin_actions = {"pin_message", "unpin_message", "create_thread", "add_role", "remove_role"}
        try:
            if action in admin_actions:
                return _action_intent_required_envelope(
                    tool_name=tool,
                    action=f"discord.{action}",
                    params={
                        "guild_id": str(arguments.get("guild_id") or ""),
                        "channel_id": str(arguments.get("channel_id") or ""),
                        "discord_user_id": str(arguments.get("user_id") or ""),
                        "role_id": str(arguments.get("role_id") or ""),
                        "message_id": str(arguments.get("message_id") or ""),
                        "name": str(arguments.get("name") or ""),
                        "auto_archive_duration": bounded_int(arguments.get("auto_archive_duration"), default=1440, minimum=60, maximum=10080),
                        "rationale": str(arguments.get("rationale") or "Discord server management actions can change channel or member state."),
                    },
                    rationale=str(arguments.get("rationale") or "Discord server management actions can change channel or member state."),
                    error="Discord server management requires ActionIntent confirmation",
                    level="platform_admin",
                    target=str(arguments.get("channel_id") or arguments.get("guild_id") or action),
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            data = DiscordServerClient().call(
                action=action,
                guild_id=str(arguments.get("guild_id") or ""),
                channel_id=str(arguments.get("channel_id") or ""),
                user_id=str(arguments.get("user_id") or ""),
                role_id=str(arguments.get("role_id") or ""),
                message_id=str(arguments.get("message_id") or ""),
                query=str(arguments.get("query") or ""),
                name=str(arguments.get("name") or ""),
                limit=bounded_int(arguments.get("limit"), default=50, minimum=1, maximum=100),
                before=str(arguments.get("before") or ""),
                after=str(arguments.get("after") or ""),
                auto_archive_duration=bounded_int(arguments.get("auto_archive_duration"), default=1440, minimum=60, maximum=10080),
            )
            success = bool(data.get("configured")) and data.get("ok") is not False
            level = "platform_admin" if action in admin_actions else "read_only"
            return _envelope(success, data=data, error=None if success else str(data.get("error") or "Discord API call failed"), tool_name=tool, level=level, idempotent=action not in admin_actions)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="platform_admin", idempotent=False)

    async def rl_list_environments(_: dict[str, Any]) -> dict[str, Any]:
        return _envelope(True, data=rl.list_environments(), tool_name="agent_rl_list_environments")

    async def rl_select_environment(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not control_context_active():
                return _stateful_action_intent_envelope(
                    tool_name="agent_rl_select_environment",
                    action="rl.select_environment",
                    params=dict(arguments or {}),
                    level="stateful",
                    target=str(arguments.get("environment") or "rl_environment"),
                    rationale=str(arguments.get("rationale") or "Selecting the RL environment changes durable training state and requires ActionIntent confirmation."),
                    error="RL environment selection requires ActionIntent confirmation",
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            return _envelope(True, data=rl.select_environment(str(arguments.get("environment") or "")), tool_name="agent_rl_select_environment", level="stateful", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_rl_select_environment", level="stateful", idempotent=False)

    async def rl_get_config(_: dict[str, Any]) -> dict[str, Any]:
        return _envelope(True, data=rl.current_config(), tool_name="agent_rl_get_config")

    async def rl_edit_config(arguments: dict[str, Any]) -> dict[str, Any]:
        if not control_context_active():
            return _stateful_action_intent_envelope(
                tool_name="agent_rl_edit_config",
                action="rl.edit_config",
                params=dict(arguments or {}),
                level="stateful",
                target="rl_config",
                rationale=str(arguments.get("rationale") or "Editing RL configuration changes durable training state and requires ActionIntent confirmation."),
                error="RL configuration edit requires ActionIntent confirmation",
                ttl_seconds=arguments.get("ttl_seconds"),
            )
        return _envelope(True, data=rl.edit_config(dict(arguments.get("config") or arguments.get("patch") or {})), tool_name="agent_rl_edit_config", level="stateful", idempotent=False)

    async def rl_start_training(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_rl_start_training"
        try:
            if not control_context_active():
                return _stateful_action_intent_envelope(
                    tool_name=tool,
                    action="rl.start_training",
                    params=dict(arguments or {}),
                    level="process_execution",
                    target=str(arguments.get("environment") or "rl_training"),
                    rationale=str(arguments.get("rationale") or "Starting RL training can launch external processes and requires ActionIntent confirmation."),
                    error="RL training start requires ActionIntent confirmation",
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            data = rl.start_training(environment=arguments.get("environment"), config_patch=dict(arguments.get("config") or {}))
            success = bool(data.get("started", True)) and data.get("configured", True) is not False
            return _envelope(success, data=data, error=None if success else "RL training credentials are not configured", tool_name=tool, level="process_execution", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="process_execution", idempotent=False)

    async def rl_check_status(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return _envelope(True, data={"run": rl.check_status(str(arguments.get("run_id") or ""))}, tool_name="agent_rl_check_status")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_rl_check_status")

    async def rl_stop_training(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not control_context_active():
                return _stateful_action_intent_envelope(
                    tool_name="agent_rl_stop_training",
                    action="rl.stop_training",
                    params=dict(arguments or {}),
                    level="process_control",
                    target=str(arguments.get("run_id") or "rl_run"),
                    rationale=str(arguments.get("rationale") or "Stopping RL training controls a managed process and requires ActionIntent confirmation."),
                    error="RL training stop requires ActionIntent confirmation",
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            return _envelope(True, data={"run": rl.stop_training(str(arguments.get("run_id") or ""))}, tool_name="agent_rl_stop_training", level="process_control", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_rl_stop_training", level="process_control", idempotent=False)

    async def rl_get_results(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return _envelope(True, data=rl.results(str(arguments.get("run_id") or "")), tool_name="agent_rl_get_results")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name="agent_rl_get_results")

    async def rl_list_runs(arguments: dict[str, Any]) -> dict[str, Any]:
        return _envelope(
            True,
            data={"runs": rl.list_runs(limit=bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=1000))},
            tool_name="agent_rl_list_runs",
        )

    async def rl_test_inference(arguments: dict[str, Any]) -> dict[str, Any]:
        if not control_context_active():
            return _stateful_action_intent_envelope(
                tool_name="agent_rl_test_inference",
                action="rl.test_inference",
                params=dict(arguments or {}),
                level="external_generation",
                target="rl_inference",
                rationale=str(arguments.get("rationale") or "RL inference tests can call an external generation endpoint and require ActionIntent confirmation."),
                error="RL inference test requires ActionIntent confirmation",
                ttl_seconds=arguments.get("ttl_seconds"),
            )
        return _envelope(True, data=rl.test_inference(str(arguments.get("prompt") or "")), tool_name="agent_rl_test_inference", level="external_generation", idempotent=False)

    async def message_send(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_message_send"
        try:
            action = str(arguments.get("action") or "send").strip().lower()
            if action == "list":
                return _envelope(True, data={"platforms": gateway.list_platforms()}, tool_name=tool, level="read_only")
            if action != "send":
                raise ValueError(f"unsupported message action: {action}")
            params = _gateway_delivery_params(arguments, default_platform="")
            return _external_action_intent_envelope(
                tool_name=tool,
                action="gateway.send_message",
                params=params,
                rationale=str(arguments.get("rationale") or "Message send model tool requires ActionIntent confirmation."),
                target=str(arguments.get("target") or ""),
                ttl_seconds=arguments.get("ttl_seconds"),
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="external_message", idempotent=False)

    async def webhook(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_webhook"
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            if action == "list":
                data = {"webhooks": webhooks.list()}
                level = "read_only"
            elif action == "subscribe":
                deliver_mode = str(arguments.get("deliver_mode") or "").strip()
                deliver_value = str(arguments.get("deliver") or "desktop_inbox")
                if deliver_mode == "direct_platform":
                    deliver_value = json.dumps(
                        {
                            "mode": "direct_platform",
                            "platform": str(arguments.get("platform") or "local"),
                            "target": str(arguments.get("target") or ""),
                            "thread_id": arguments.get("thread_id"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                params = {
                    "name": str(arguments.get("name") or ""),
                    "events": [str(item) for item in list(arguments.get("events") or [])],
                    "prompt": str(arguments.get("prompt") or ""),
                    "deliver": deliver_value,
                    "secret": arguments.get("secret"),
                    "user_id": arguments.get("user_id"),
                    "session_id": arguments.get("session_id"),
                }
                if not control_context_active():
                    return _stateful_action_intent_envelope(
                        tool_name=tool,
                        action="webhook.subscribe",
                        params=params,
                        level="stateful",
                        target=str(arguments.get("name") or "webhook"),
                        rationale=str(arguments.get("rationale") or "Webhook subscriptions persist future external triggers and require ActionIntent confirmation."),
                        error="Webhook subscription requires ActionIntent confirmation",
                        ttl_seconds=arguments.get("ttl_seconds"),
                    )
                data = {
                    "webhook": webhooks.subscribe(
                        name=params["name"],
                        events=list(params["events"]),
                        prompt=params["prompt"],
                        deliver=str(params["deliver"] or "desktop_inbox"),
                        secret=params.get("secret"),
                    )
                }
                level = "stateful"
            elif action == "remove":
                if not control_context_active():
                    return _stateful_action_intent_envelope(
                        tool_name=tool,
                        action="webhook.remove",
                        params={
                            "webhook_id": str(arguments.get("webhook_id") or ""),
                            "user_id": arguments.get("user_id"),
                            "session_id": arguments.get("session_id"),
                        },
                        level="stateful",
                        target=str(arguments.get("webhook_id") or "webhook"),
                        rationale=str(arguments.get("rationale") or "Webhook removal changes durable trigger configuration and requires ActionIntent confirmation."),
                        error="Webhook removal requires ActionIntent confirmation",
                        ttl_seconds=arguments.get("ttl_seconds"),
                    )
                data = {"deleted": webhooks.remove(str(arguments.get("webhook_id") or ""))}
                level = "stateful"
            elif action == "trigger":
                return _stateful_action_intent_envelope(
                    tool_name=tool,
                    action="webhook.trigger",
                    params={
                        "webhook_id": str(arguments.get("webhook_id") or ""),
                        "event": str(arguments.get("event") or "event"),
                        "payload": dict(arguments.get("payload") or {}),
                        "signature": arguments.get("signature"),
                        "session_id": arguments.get("session_id"),
                        "user_id": arguments.get("user_id"),
                    },
                    level="external_message",
                    rationale=str(arguments.get("rationale") or "Webhook trigger model tool requires ActionIntent confirmation."),
                    target=str(arguments.get("webhook_id") or ""),
                    error="Webhook trigger requires ActionIntent confirmation",
                    ttl_seconds=arguments.get("ttl_seconds"),
                )
            else:
                raise ValueError(f"unsupported webhook action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=action, idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    return {
        **web_handlers,
        **planning_handlers,
        "agent_skill_list": skill_list,
        "agent_skill_view": skill_view,
        "agent_skill_save": skill_save,
        "agent_skill_manage": skill_manage,
        "agent_plugin_list": plugin_list,
        "agent_plugin_set_enabled": plugin_set_enabled,
        "agent_plugin_manage": plugin_manage,
        "agent_mcp_manage": mcp_manage,
        "agent_model_manage": model_manage,
        "agent_memory_manage": memory_manage,
        "agent_acp_manage": acp_manage,
        "agent_security_scan": security_scan,
        "agent_skill_pack_manage": skill_pack_manage,
        "agent_terminal_backends": terminal_backends,
        "agent_tui_status": tui_status,
        "agent_gateway_status": gateway_status,
        "agent_gateway_platforms": gateway_platforms,
        "agent_gateway_send_message": gateway_send_message,
        "agent_gateway_history": gateway_history,
        "agent_gateway_pairing": gateway_pairing,
        "agent_gateway_directory": gateway_directory,
        "agent_gateway_direct_deliver": gateway_direct_deliver,
        "agent_session_handoff": session_handoff,
        "agent_learning_status": learning_status,
        "agent_learning_review": learning_review,
        "agent_learning_apply": learning_apply,
        "agent_skill_reflect": skill_reflect,
        "agent_ha_list_entities": ha_list_entities,
        "agent_ha_get_state": ha_get_state,
        "agent_ha_list_services": ha_list_services,
        "agent_ha_list_events": ha_list_events,
        "agent_ha_list_registry": ha_list_registry,
        "agent_ha_call_service": ha_call_service,
        "agent_feishu_doc_read": feishu_doc_read,
        "agent_feishu_drive_list_comments": lambda arguments: feishu_comments(arguments, tool_name="agent_feishu_drive_list_comments"),
        "agent_feishu_drive_list_comment_replies": lambda arguments: feishu_comments(arguments, tool_name="agent_feishu_drive_list_comment_replies"),
        "agent_feishu_drive_reply_comment": lambda arguments: feishu_comments(arguments, tool_name="agent_feishu_drive_reply_comment"),
        "agent_feishu_drive_add_comment": lambda arguments: feishu_comments(arguments, tool_name="agent_feishu_drive_add_comment"),
        "agent_discord_channel_send": discord_channel_send,
        "agent_discord_server": discord_server,
        "agent_rl_list_environments": rl_list_environments,
        "agent_rl_select_environment": rl_select_environment,
        "agent_rl_get_config": rl_get_config,
        "agent_rl_edit_config": rl_edit_config,
        "agent_rl_start_training": rl_start_training,
        "agent_rl_check_status": rl_check_status,
        "agent_rl_stop_training": rl_stop_training,
        "agent_rl_get_results": rl_get_results,
        "agent_rl_list_runs": rl_list_runs,
        "agent_rl_test_inference": rl_test_inference,
        **media_handlers,
        "agent_message_send": message_send,
        "agent_webhook": webhook,
    }
