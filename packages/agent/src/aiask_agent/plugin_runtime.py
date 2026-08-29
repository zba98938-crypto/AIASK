from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from .numeric import bounded_float, bounded_int
from .paths import aiask_agent_home


def _safe_slug(value: str) -> str:
    import re

    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return slug[:120] or f"plugin-{uuid4().hex[:8]}"


class PluginToolNameCollision(ValueError):
    def __init__(self, wrapped_name: str, definitions: list[dict[str, Any]]) -> None:
        self.wrapped_name = wrapped_name
        self.definitions = [dict(item) for item in definitions]
        targets = ", ".join(
            f"{item.get('plugin')}.{item.get('kind')}.{item.get('tool')}"
            for item in self.definitions
        )
        super().__init__(f"plugin tool wrapper name collision for {wrapped_name}: {targets}")


class NativePluginManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or aiask_agent_home() / "plugins"

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        items: list[dict[str, Any]] = []
        for manifest in sorted(self.root.glob("*/plugin.json")):
            data = self._read_manifest(manifest)
            data["path"] = str(manifest)
            data.setdefault("name", manifest.parent.name)
            data.setdefault("enabled", False)
            data.setdefault("hooks", [])
            data.setdefault("commands", [])
            data.setdefault("dashboard", {})
            items.append(data)
        return items

    def get(self, name: str) -> dict[str, Any] | None:
        path = self.root / _safe_slug(name) / "plugin.json"
        if not path.exists():
            return None
        data = self._read_manifest(path)
        data["path"] = str(path)
        return data

    def set_enabled(self, name: str, enabled: bool, *, description: str | None = None) -> dict[str, Any]:
        plugin_name = _safe_slug(name)
        path = self.root / plugin_name / "plugin.json"
        data = self._read_manifest(path) if path.exists() else {}
        data.update({"name": plugin_name, "enabled": bool(enabled)})
        if description is not None:
            data["description"] = description
        data.setdefault("tools", [])
        data.setdefault("hooks", [])
        data.setdefault("commands", [])
        data.setdefault("dashboard", {})
        self._write_manifest(path, data)
        data["path"] = str(path)
        return data

    def update(self, name: str, *, manifest: dict[str, Any]) -> dict[str, Any]:
        plugin_name = _safe_slug(name)
        path = self.root / plugin_name / "plugin.json"
        data = self._read_manifest(path) if path.exists() else {"name": plugin_name}
        data.update(dict(manifest or {}))
        data["name"] = plugin_name
        data.setdefault("enabled", False)
        data.setdefault("tools", [])
        data.setdefault("hooks", [])
        data.setdefault("commands", [])
        data.setdefault("dashboard", {})
        self._write_manifest(path, data)
        data["path"] = str(path)
        return data

    def enabled_hooks(self, hook_name: str) -> list[dict[str, Any]]:
        hooks: list[dict[str, Any]] = []
        for plugin in self.list():
            if not plugin.get("enabled"):
                continue
            for hook in list(plugin.get("hooks") or []):
                if isinstance(hook, dict) and hook.get("name") == hook_name:
                    hooks.append({"plugin": plugin["name"], **hook})
        return hooks

    def _all_tool_definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for plugin in self.list():
            if not plugin.get("enabled"):
                continue
            plugin_name = _safe_slug(str(plugin.get("name") or "plugin"))
            for tool in list(plugin.get("tools") or []):
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                raw_name = _safe_slug(str(tool.get("name") or "tool")).replace("-", "_")
                wrapped_name = f"agent_plugin_{plugin_name.replace('-', '_')}_{raw_name}"
                definitions.append(
                    {
                        "name": wrapped_name,
                        "plugin": plugin_name,
                        "tool": str(tool.get("name") or ""),
                        "description": str(tool.get("description") or f"AIASK plugin tool {plugin_name}.{tool.get('name')}"),
                        "parameters": dict(tool.get("parameters") or {"type": "object", "properties": {}, "additionalProperties": True}),
                        "side_effect": str(tool.get("side_effect") or "stateful"),
                        "runner": dict(tool.get("runner") or plugin.get("runner") or {}),
                        "kind": "tool",
                    }
                )
            for command in list(plugin.get("commands") or []):
                if not isinstance(command, dict) or not command.get("name"):
                    continue
                raw_name = _safe_slug(str(command.get("name") or "command")).replace("-", "_")
                wrapped_name = f"agent_plugin_{plugin_name.replace('-', '_')}_command_{raw_name}"
                definitions.append(
                    {
                        "name": wrapped_name,
                        "plugin": plugin_name,
                        "tool": str(command.get("name") or ""),
                        "description": str(command.get("description") or f"AIASK plugin command {plugin_name}.{command.get('name')}"),
                        "parameters": dict(command.get("parameters") or {"type": "object", "properties": {}, "additionalProperties": True}),
                        "side_effect": str(command.get("side_effect") or "stateful"),
                        "runner": dict(command.get("runner") or plugin.get("runner") or {}),
                        "kind": "command",
                    }
                )
        return definitions

    def tool_name_collisions(self) -> list[dict[str, Any]]:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for definition in self._all_tool_definitions():
            by_name.setdefault(str(definition.get("name") or ""), []).append(definition)
        return [
            {"name": name, "definitions": [dict(item) for item in definitions]}
            for name, definitions in sorted(by_name.items())
            if name and len(definitions) > 1
        ]

    def tool_definitions(self) -> list[dict[str, Any]]:
        definitions = self._all_tool_definitions()
        collisions = {str(item.get("name") or "") for item in self.tool_name_collisions()}
        return [definition for definition in definitions if str(definition.get("name") or "") not in collisions]

    async def call_tool(self, wrapped_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        matches = [
            definition
            for definition in self._all_tool_definitions()
            if definition["name"] == wrapped_name
        ]
        if len(matches) > 1:
            raise PluginToolNameCollision(wrapped_name, matches)
        for definition in matches:
            plugin = self.get(definition["plugin"]) or {}
            spec = self._command_spec(plugin, definition["tool"]) if definition.get("kind") == "command" else self._tool_spec(plugin, definition["tool"])
            runner = dict(spec.get("runner") or plugin.get("runner") or {})
            if not runner:
                return {
                    "plugin": definition["plugin"],
                    "tool": definition["tool"],
                    "kind": definition.get("kind") or "tool",
                    "arguments": dict(arguments or {}),
                    "configured": False,
                    "runner": None,
                    "note": "plugin surface is registered but no executable runner is declared",
                }
            result = await self._run_runner(
                runner,
                plugin=plugin,
                tool=definition["tool"],
                arguments=dict(arguments or {}),
            )
            return {
                "plugin": definition["plugin"],
                "tool": definition["tool"],
                "kind": definition.get("kind") or "tool",
                "arguments": dict(arguments or {}),
                "configured": True,
                "runner": self._runner_type(runner),
                "result": result,
            }
        raise ValueError(f"plugin tool is not configured: {wrapped_name}")

    def list_commands(self, name: str) -> list[dict[str, Any]]:
        plugin = self.get(name)
        if not plugin:
            return []
        return [
            {
                "name": str(command.get("name") or ""),
                "description": str(command.get("description") or ""),
                "parameters": dict(command.get("parameters") or {"type": "object", "properties": {}, "additionalProperties": True}),
                "runner": self._runner_type(dict(command.get("runner") or plugin.get("runner") or {})) or None,
                "side_effect": str(command.get("side_effect") or "stateful"),
            }
            for command in list(plugin.get("commands") or [])
            if isinstance(command, dict) and command.get("name")
        ]

    async def call_command(self, plugin_name: str, command_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        plugin = self.get(plugin_name)
        if not plugin:
            raise ValueError(f"plugin not found: {plugin_name}")
        wrapped = f"agent_plugin_{_safe_slug(str(plugin.get('name') or plugin_name)).replace('-', '_')}_command_{_safe_slug(command_name).replace('-', '_')}"
        return await self.call_tool(wrapped, arguments)

    async def pre_llm_call(self, *, messages: list[dict[str, Any]], model: str, tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        current = [dict(item) for item in messages]
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("pre_llm_call"):
            output = await self._run_hook(hook, {"messages": current, "model": model, "tools": tools})
            events.append({"hook": "pre_llm_call", "plugin": hook.get("plugin"), "result": self._summarize_result(output)})
            if isinstance(output, dict) and isinstance(output.get("messages"), list):
                current = [dict(item) for item in output["messages"] if isinstance(item, dict)]
        return current, events

    async def post_llm_call(self, *, messages: list[dict[str, Any]], model: str, response: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("post_llm_call"):
            output = await self._run_hook(hook, {"messages": messages, "model": model, "response": response})
            events.append({"hook": "post_llm_call", "plugin": hook.get("plugin"), "result": self._summarize_result(output)})
        return events

    def transform_tool_result(self, *, tool_name: str, result: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        current = dict(result or {})
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("transform_tool_result"):
            if not self._hook_matches_tool(hook, tool_name):
                continue
            prefix = str(hook.get("prefix") or "")
            output: Any = None
            runner = self._runner_from_hook(hook)
            if runner:
                try:
                    output = self._run_subprocess(
                        runner,
                        self.get(str(hook.get("plugin") or "")) or {"name": hook.get("plugin")},
                        str(hook.get("name") or "transform_tool_result"),
                        {"tool_name": tool_name, "result": current},
                    ) if self._runner_type(runner) in {"subprocess", "shell", "shell_hook"} else None
                except Exception as exc:
                    events.append({"hook": "transform_tool_result", "plugin": hook.get("plugin"), "tool": tool_name, "error": str(exc)})
            if isinstance(output, dict) and isinstance(output.get("result"), dict):
                current = dict(output["result"])
            elif prefix and isinstance(current.get("data"), dict):
                data = dict(current["data"])
                data["plugin_prefix"] = prefix
                current["data"] = data
            events.append({"hook": "transform_tool_result", "plugin": hook.get("plugin"), "tool": tool_name, "result": self._summarize_result(output) if output is not None else None})
        return current, events

    async def pre_tool_call(self, *, tool_name: str, arguments: dict[str, Any], session_id: str, run_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("pre_tool_call"):
            if not self._hook_matches_tool(hook, tool_name):
                continue
            output = await self._run_hook(
                hook,
                {"tool_name": tool_name, "tool_input": dict(arguments or {}), "session_id": session_id, "run_id": run_id},
            )
            summary = {"hook": "pre_tool_call", "plugin": hook.get("plugin"), "tool": tool_name, "result": self._summarize_result(output)}
            events.append(summary)
            if isinstance(output, dict):
                decision = str(output.get("decision") or output.get("action") or "").strip().lower()
                if decision == "block":
                    return {
                        "blocked": True,
                        "reason": str(output.get("reason") or output.get("message") or "blocked by plugin hook"),
                        "plugin": hook.get("plugin"),
                        "hook": hook.get("name"),
                    }, events
        return None, events

    async def post_tool_call(self, *, tool_name: str, arguments: dict[str, Any], result: dict[str, Any], session_id: str, run_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("post_tool_call"):
            if not self._hook_matches_tool(hook, tool_name):
                continue
            output = await self._run_hook(
                hook,
                {"tool_name": tool_name, "tool_input": dict(arguments or {}), "result": result, "session_id": session_id, "run_id": run_id},
            )
            events.append({"hook": "post_tool_call", "plugin": hook.get("plugin"), "tool": tool_name, "result": self._summarize_result(output)})
        return events

    async def on_session_start(self, *, session_id: str, run_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("on_session_start"):
            output = await self._run_hook(hook, {"session_id": session_id, "run_id": run_id})
            events.append({"hook": "on_session_start", "plugin": hook.get("plugin"), "result": self._summarize_result(output)})
        return events

    async def on_session_end(self, *, session_id: str, run_id: str, status: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("on_session_end"):
            output = await self._run_hook(hook, {"session_id": session_id, "run_id": run_id, "status": status})
            events.append({"hook": "on_session_end", "plugin": hook.get("plugin"), "result": self._summarize_result(output)})
        return events

    async def transform_terminal_output(self, *, command: str, output: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        current = dict(output or {})
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("transform_terminal_output"):
            result = await self._run_hook(hook, {"command": command, "output": current})
            if isinstance(result, dict):
                if isinstance(result.get("terminal_output"), str):
                    current["stdout"] = str(result["terminal_output"])
                if isinstance(result.get("result"), dict):
                    current.update(dict(result["result"]))
            events.append({"hook": "transform_terminal_output", "plugin": hook.get("plugin"), "result": self._summarize_result(result)})
        return current, events

    async def on_session_finalize(self, *, session_id: str, run_id: str, status: str, response: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("on_session_finalize"):
            output = await self._run_hook(hook, {"session_id": session_id, "run_id": run_id, "status": status, "response": response})
            events.append({"hook": "on_session_finalize", "plugin": hook.get("plugin"), "result": self._summarize_result(output)})
        return events

    async def on_session_reset(self, *, session_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for hook in self.enabled_hooks("on_session_reset"):
            output = await self._run_hook(hook, {"session_id": session_id})
            events.append({"hook": "on_session_reset", "plugin": hook.get("plugin"), "result": self._summarize_result(output)})
        return events

    def readiness(self) -> dict[str, Any]:
        plugins = self.list()
        surfaces: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for plugin in plugins:
            if not plugin.get("enabled"):
                continue
            for definition in self.tool_definitions():
                if definition.get("plugin") != plugin.get("name"):
                    continue
                runner = dict(definition.get("runner") or {})
                runner_type = self._runner_type(runner)
                item = {
                    "plugin": plugin.get("name"),
                    "name": definition.get("name"),
                    "kind": definition.get("kind") or "tool",
                    "runner": runner_type or None,
                    "configured": bool(runner_type),
                }
                if not runner_type:
                    failures.append({**item, "reason": "missing_runner"})
                surfaces.append(item)
        for collision in self.tool_name_collisions():
            failures.append(
                {
                    "name": collision.get("name"),
                    "kind": "collision",
                    "configured": False,
                    "reason": "wrapped_name_collision",
                    "definitions": collision.get("definitions") or [],
                }
            )
        return {
            "count": len(plugins),
            "enabled_count": sum(1 for item in plugins if item.get("enabled")),
            "surfaces": surfaces,
            "failures": failures,
            "configured": not failures,
            "supported_hooks": [
                "pre_tool_call",
                "post_tool_call",
                "pre_llm_call",
                "post_llm_call",
                "on_session_start",
                "on_session_end",
                "on_session_finalize",
                "on_session_reset",
                "transform_tool_result",
                "transform_terminal_output",
            ],
        }

    async def _run_hook(self, hook: dict[str, Any], payload: dict[str, Any]) -> Any:
        runner = self._runner_from_hook(hook)
        if runner:
            plugin = self.get(str(hook.get("plugin") or "")) or {"name": hook.get("plugin")}
            return await self._run_runner(runner, plugin=plugin, tool=str(hook.get("name") or "hook"), arguments=payload)
        return {
            "skipped": True,
            "reason": "no_runner",
            "configured": False,
            "hook": hook.get("name"),
            "plugin": hook.get("plugin"),
            "note": "hook has no executable runner",
        }

    async def _run_runner(self, runner: dict[str, Any], *, plugin: dict[str, Any], tool: str, arguments: dict[str, Any]) -> Any:
        runner_type = self._runner_type(runner)
        if runner_type == "python_module":
            return await self._run_python_module(runner, plugin=plugin, tool=tool, arguments=arguments)
        if runner_type == "http":
            return await asyncio.to_thread(self._run_http, runner, plugin, tool, arguments)
        if runner_type in {"subprocess", "shell", "shell_hook"}:
            return await asyncio.to_thread(self._run_subprocess, runner, plugin, tool, arguments)
        raise ValueError(f"unsupported plugin runner: {runner_type}")

    async def _run_python_module(self, runner: dict[str, Any], *, plugin: dict[str, Any], tool: str, arguments: dict[str, Any]) -> Any:
        module_name = str(runner.get("module") or "").strip()
        function_name = str(runner.get("function") or runner.get("callable") or "run").strip()
        if not module_name:
            raise ValueError("python_module runner requires module")
        plugin_dir = Path(str(plugin.get("path") or "")).parent if plugin.get("path") else self.root / _safe_slug(str(plugin.get("name") or ""))
        module_file = plugin_dir.joinpath(*module_name.split(".")).with_suffix(".py")
        package_init = plugin_dir.joinpath(*module_name.split("."), "__init__.py")
        source_path = module_file if module_file.is_file() else package_init if package_init.is_file() else None
        if source_path is None:
            raise FileNotFoundError(f"plugin python module must resolve inside the plugin directory: {module_name}")
        source_path = source_path.resolve()
        source_path.relative_to(plugin_dir.resolve())
        identity = hashlib.sha256(f"{plugin_dir.resolve()}:{module_name}".encode("utf-8")).hexdigest()[:16]
        isolated_name = f"_aiask_plugin_{identity}"
        spec = importlib.util.spec_from_file_location(
            isolated_name,
            source_path,
            submodule_search_locations=[str(source_path.parent)] if source_path.name == "__init__.py" else None,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin python module: {source_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[isolated_name] = module
        sys.path.insert(0, str(plugin_dir))
        try:
            spec.loader.exec_module(module)
            fn = getattr(module, function_name)
            payload = {"plugin": plugin.get("name"), "tool": tool, "arguments": arguments}
            value = fn(payload)
            if asyncio.iscoroutine(value):
                value = await value
            return value
        finally:
            sys.modules.pop(isolated_name, None)
            try:
                sys.path.remove(str(plugin_dir))
            except ValueError:
                pass

    def _run_http(self, runner: dict[str, Any], plugin: dict[str, Any], tool: str, arguments: dict[str, Any]) -> Any:
        url = str(runner.get("url") or "").strip()
        if not url:
            raise ValueError("http runner requires url")
        self._validate_http_target(url)
        headers = {"Content-Type": "application/json", "User-Agent": "AIASK-Agent-Plugin/0.1"}
        for key, value in dict(runner.get("headers") or {}).items():
            headers[str(key)] = str(value)
        body = json.dumps({"plugin": plugin.get("name"), "tool": tool, "arguments": arguments}, ensure_ascii=False).encode("utf-8")
        timeout = bounded_float(runner.get("timeout_seconds"), default=30.0, minimum=1.0, maximum=300.0)
        request = Request(url, data=body, headers=headers, method=str(runner.get("method") or "POST").upper())
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(bounded_int(runner.get("max_bytes"), default=1048576, minimum=1, maximum=5 * 1024 * 1024))
            text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except Exception:
            return {"text": text}

    def _run_subprocess(self, runner: dict[str, Any], plugin: dict[str, Any], tool: str, arguments: dict[str, Any]) -> Any:
        command = runner.get("command")
        if not command:
            raise ValueError("subprocess runner requires command")
        plugin_dir = Path(str(plugin.get("path") or "")).parent if plugin.get("path") else self.root / _safe_slug(str(plugin.get("name") or ""))
        cwd = self._safe_subprocess_cwd(plugin_dir, runner.get("cwd"))
        env = self._subprocess_env(runner)
        timeout = bounded_float(runner.get("timeout_seconds"), default=30.0, minimum=1.0, maximum=300.0)
        args = [str(item) for item in command] if isinstance(command, list) else str(command)
        proc = subprocess.run(
            args,
            input=json.dumps({"plugin": plugin.get("name"), "tool": tool, "arguments": arguments}, ensure_ascii=True),
            text=True,
            cwd=str(cwd),
            env=env,
            shell=not isinstance(command, list),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        payload = {"returncode": proc.returncode, "stdout": proc.stdout[-200000:], "stderr": proc.stderr[-200000:]}
        try:
            payload["json"] = json.loads(proc.stdout)
        except Exception:
            pass
        if proc.returncode != 0:
            raise RuntimeError(f"plugin subprocess failed with code {proc.returncode}: {proc.stderr[-2000:]}")
        return payload.get("json", payload)

    def _safe_subprocess_cwd(self, plugin_dir: Path, raw: Any) -> Path:
        base = plugin_dir.expanduser().resolve()
        path = base if not raw else (base / str(raw)).expanduser().resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise PermissionError("plugin subprocess cwd must stay inside the plugin directory") from exc
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path

    @staticmethod
    def _subprocess_env(runner: dict[str, Any]) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        for key in list(runner.get("env_allowlist") or []):
            token = str(key or "").strip()
            if token and token in os.environ:
                env[token] = os.environ[token]
        return env

    @staticmethod
    def _validate_http_target(url: str) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        allowed = {item.strip().lower() for item in str(os.getenv("AIASK_PLUGIN_HTTP_ALLOWLIST", "")).split(",") if item.strip()}
        if host in {"127.0.0.1", "localhost", "::1"} or host.lower() in allowed:
            return
        raise PermissionError("plugin http runner is restricted to loopback or AIASK_PLUGIN_HTTP_ALLOWLIST hosts")

    @staticmethod
    def _runner_type(runner: dict[str, Any]) -> str:
        explicit = str(runner.get("type") or runner.get("kind") or "").strip().lower()
        if explicit:
            return explicit
        if runner.get("module"):
            return "python_module"
        if runner.get("url"):
            return "http"
        if runner.get("command"):
            return "subprocess"
        return ""

    @staticmethod
    def _runner_from_hook(hook: dict[str, Any]) -> dict[str, Any]:
        runner = dict(hook.get("runner") or {})
        if runner:
            return runner
        command = hook.get("command")
        if command:
            return {
                "type": str(hook.get("type") or "subprocess"),
                "command": command,
                "timeout_seconds": hook.get("timeout_seconds") or hook.get("timeout"),
                "env_allowlist": hook.get("env_allowlist") or [],
            }
        return {}

    @staticmethod
    def _hook_matches_tool(hook: dict[str, Any], tool_name: str) -> bool:
        matcher = hook.get("tool") or hook.get("tool_name") or hook.get("matcher")
        if not matcher:
            return True
        if isinstance(matcher, list):
            return tool_name in {str(item) for item in matcher}
        raw = str(matcher)
        if raw == tool_name:
            return True
        try:
            import re

            return re.fullmatch(raw, tool_name) is not None
        except re.error:
            return False

    @staticmethod
    def _tool_spec(plugin: dict[str, Any], name: str) -> dict[str, Any]:
        for tool in list(plugin.get("tools") or []):
            if isinstance(tool, dict) and str(tool.get("name") or "") == name:
                return tool
        return {}

    @staticmethod
    def _command_spec(plugin: dict[str, Any], name: str) -> dict[str, Any]:
        for command in list(plugin.get("commands") or []):
            if isinstance(command, dict) and str(command.get("name") or "") == name:
                return command
        return {}

    @staticmethod
    def _summarize_result(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {key: value.get(key) for key in list(value)[:8]}
        return {"type": type(value).__name__}

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _write_manifest(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
