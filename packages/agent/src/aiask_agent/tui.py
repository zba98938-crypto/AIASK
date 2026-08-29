from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .env_config import load_project_env


TUI_COMMANDS = (
    "/help",
    "/model",
    "/tools",
    "/sessions",
    "/new",
    "/stop",
    "/steer",
    "/skills",
    "/approvals",
    "/resume",
    "/undo",
    "/rollback",
    "/artifacts",
    "/sources",
)


@dataclass(frozen=True)
class TUISlashCommand:
    command: str
    args: str = ""
    tokens: tuple[str, ...] = ()
    raw: str = ""


@dataclass
class TUIController:
    model: str = ""
    toolset: str = "hermes_full"
    session_id: str | None = None
    current_run_id: str | None = None
    timeline: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    last_connection_error: str | None = None

    commands: tuple[str, ...] = TUI_COMMANDS

    def parse_slash_command(self, text: str) -> TUISlashCommand | None:
        raw = str(text or "").strip()
        if not raw.startswith("/"):
            return None
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        return TUISlashCommand(command=command, args=args, tokens=tuple(args.split()), raw=raw)

    def autocomplete(self, prefix: str) -> list[str]:
        token = str(prefix or "").strip().lower()
        return [command for command in self.commands if command.startswith(token or "/")]

    def apply_local_command(self, parsed: TUISlashCommand) -> dict[str, Any]:
        if parsed.command == "/model":
            if parsed.tokens:
                self.model = parsed.tokens[0]
            return {"model": self.model or "(server default)"}
        if parsed.command == "/new":
            self.session_id = None
            self.current_run_id = None
            self.timeline.clear()
            return {"session_id": None, "status": "new_session"}
        if parsed.command == "/resume":
            if not parsed.tokens:
                return {"error": "session_id is required"}
            self.session_id = parsed.tokens[0]
            return {"session_id": self.session_id, "status": "resumed"}
        if parsed.command == "/undo":
            if not self.session_id:
                return {"error": "session_id is required"}
            try:
                turns = int(parsed.tokens[0]) if parsed.tokens else 1
            except (TypeError, ValueError):
                return {"error": "turns must be a positive integer"}
            if turns < 1:
                return {"error": "turns must be a positive integer"}
            return {"session_id": self.session_id, "turns": turns, "status": "pending_remote_undo"}
        if parsed.command == "/rollback":
            if not parsed.tokens:
                return {"error": "checkpoint_id or latest <path> is required"}
            if parsed.tokens[0] == "latest":
                if len(parsed.tokens) < 2:
                    return {"error": "path is required for latest rollback"}
                return {"path": " ".join(parsed.tokens[1:]), "status": "pending_remote_rollback"}
            return {"checkpoint_id": parsed.tokens[0], "status": "pending_remote_rollback"}
        return {}

    def reduce_sse_event(self, event: dict[str, Any]) -> dict[str, Any]:
        item = dict(event or {})
        event_type = str(item.get("event") or item.get("event_type") or item.get("type") or "event")
        data = item.get("data") if "data" in item else item
        reduced = {"event": event_type, "data": data}
        self.timeline.append(reduced)
        self.timeline[:] = self.timeline[-500:]
        if event_type in {"approval.pending", "approval"}:
            self.reduce_approval_event(reduced)
        if event_type in {"response.completed", "run.completed"}:
            run_id = dict(data or {}).get("run_id") if isinstance(data, dict) else None
            if run_id:
                self.current_run_id = str(run_id)
        return reduced

    def reduce_approval_event(self, event: dict[str, Any]) -> dict[str, Any]:
        data = dict(event.get("data") or {}) if isinstance(event.get("data"), dict) else dict(event or {})
        approval = data.get("approval") if isinstance(data.get("approval"), dict) else data
        item = {"event": event.get("event") or "approval.pending", "approval": approval}
        self.approvals.append(item)
        self.approvals[:] = self.approvals[-100:]
        return item

    def features(self) -> dict[str, Any]:
        return {
            "slash_parser": True,
            "slash_autocomplete": True,
            "model_state": True,
            "toolset_state": True,
            "session_resume": True,
            "session_undo": True,
            "file_rollback": True,
            "artifact_browser": True,
            "source_browser": True,
            "sse_event_reducer": True,
            "approval_reducer": True,
            "commands": list(self.commands),
            "last_connection_error": self.last_connection_error,
        }


def status() -> dict[str, Any]:
    textual_available = importlib.util.find_spec("textual") is not None
    controller = TUIController()
    return {
        "object": "aiask.tui_status",
        "implementation": "aiask_native",
        "textual_available": textual_available,
        "controller": controller.features(),
        "features": {
            "multiline_input": True,
            "slash_command_autocomplete": True,
            "conversation_history": True,
            "interrupt_stop_steer": True,
            "run_event_timeline": True,
            "streaming_tool_output": True,
            "approval_prompts": True,
            "session_resume": True,
            "session_undo": True,
            "file_rollback": True,
            "artifact_browser": True,
            "source_browser": True,
        },
    }


def run() -> None:
    if importlib.util.find_spec("textual") is None:
        raise SystemExit("AIASK TUI requires optional dependency: textual")
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.widgets import Footer, Header, Input, Log

    class APIClient:
        def __init__(self) -> None:
            self.base_url = str(os.getenv("AIASK_AGENT_URL") or "http://127.0.0.1:8765").rstrip("/")
            self.token = str(os.getenv("AIASK_AGENT_API_TOKEN") or "").strip()
            self.control_token = str(os.getenv("AIASK_AGENT_CONTROL_TOKEN") or os.getenv("AIASK_LOCAL_CONTROL_TOKEN") or "").strip()

        def headers(self, *, control: bool = False) -> dict[str, str]:
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            if control and self.control_token:
                headers["X-AIASK-Agent-Control-Token"] = self.control_token
            return headers

        def request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, control: bool = False) -> Any:
            body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(f"{self.base_url}{path}", data=body, headers=self.headers(control=control), method=method)
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    raw = response.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    class AIASKTUI(App[None]):
        CSS = "Log { height: 1fr; } Input { dock: bottom; }"
        BINDINGS = [
            ("ctrl+c", "stop_current", "Stop"),
            ("ctrl+n", "new_session", "New"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.api = APIClient()
            load_project_env()
            self.controller = TUIController(model=os.getenv("AIASK_AGENT_MODEL", ""))

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical():
                yield Log(id="log")
                yield Input(placeholder="AIASK instruction. Slash: /help /model /tools /sessions /new /stop /steer /skills /undo /rollback")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one(Log).write_line(f"AIASK native TUI connected to {self.api.base_url}")

        def on_input_submitted(self, event: Input.Submitted) -> None:
            value = event.value.strip()
            if value:
                self.run_worker(self.handle_input(value), exclusive=False)
            event.input.value = ""

        async def handle_input(self, value: str) -> None:
            log = self.query_one(Log)
            try:
                if value.startswith("/"):
                    await self.handle_command(value)
                    return
                log.write_line(f"> {value}")
                payload: dict[str, Any] = {"input": value}
                if self.controller.session_id:
                    payload["session_id"] = self.controller.session_id
                if self.controller.model:
                    payload["model"] = self.controller.model
                response = await self.run_worker_thread(lambda: self.api.request("POST", "/v1/responses", payload))
                self.controller.session_id = response.get("metadata", {}).get("session_id") or response.get("session_id") or self.controller.session_id
                self.controller.current_run_id = response.get("metadata", {}).get("run_id") or response.get("run_id")
                text = response.get("output_text") or response.get("content") or json.dumps(response, ensure_ascii=False)[:4000]
                log.write_line(str(text))
                if self.controller.current_run_id:
                    await self.render_timeline(self.controller.current_run_id)
            except Exception as exc:
                self.controller.last_connection_error = str(exc)
                log.write_line(f"error: {exc}")

        async def handle_command(self, value: str) -> None:
            log = self.query_one(Log)
            parsed = self.controller.parse_slash_command(value)
            if parsed is None:
                return
            parts = value.split(maxsplit=2)
            command = parsed.command
            if command == "/help":
                log.write_line(" ".join(self.controller.commands))
            elif command == "/model":
                log.write_line("model: " + str(self.controller.apply_local_command(parsed).get("model")))
            elif command == "/tools":
                data = await self.run_worker_thread(lambda: self.api.request("GET", "/v1/hermes/tools", control=True))
                names = [item.get("name") for item in list(data.get("data") or [])]
                log.write_line("tools: " + ", ".join(str(name) for name in names[:80]))
            elif command == "/sessions":
                data = await self.run_worker_thread(lambda: self.api.request("GET", "/v1/hermes/sessions", control=True))
                sessions = list(data.get("data") or [])
                log.write_line("sessions: " + ", ".join(str(item.get("session_id")) for item in sessions[:20]))
            elif command == "/skills":
                data = await self.run_worker_thread(lambda: self.api.request("GET", "/v1/skills", control=True))
                skills = list(dict(data.get("data") or {}).get("skills") or [])
                log.write_line("skills: " + ", ".join(str(item.get("name")) for item in skills[:30]))
            elif command == "/approvals":
                data = await self.run_worker_thread(lambda: self.api.request("GET", "/v1/approvals", control=True))
                approvals = list(data.get("data") or [])
                log.write_line("approvals: " + ", ".join(str(item.get("approval_id") or item.get("intent_id")) for item in approvals[:30]))
            elif command == "/artifacts":
                run_id = parsed.tokens[0] if parsed.tokens else self.controller.current_run_id
                if not run_id:
                    log.write_line("usage: /artifacts <run_id> (or run a task first)")
                    return
                data = await self.run_worker_thread(lambda: self.api.request("GET", f"/v1/runs/{run_id}/artifacts?limit=20", control=True))
                artifacts = list(data.get("data") or [])
                if not artifacts:
                    log.write_line("artifacts: none")
                    return
                for item in artifacts[:20]:
                    log.write_line(
                        "artifact: "
                        + str(item.get("artifact_id") or "")
                        + " "
                        + str(item.get("kind") or "")
                        + " "
                        + str(item.get("title") or item.get("path") or item.get("uri") or "")
                    )
            elif command == "/sources":
                run_id = parsed.tokens[0] if parsed.tokens else self.controller.current_run_id
                if not run_id:
                    log.write_line("usage: /sources <run_id> (or run a task first)")
                    return
                data = await self.run_worker_thread(lambda: self.api.request("GET", f"/v1/runs/{run_id}/sources?limit=20", control=True))
                sources = list(data.get("data") or [])
                if not sources:
                    log.write_line("sources: none")
                    return
                for item in sources[:20]:
                    log.write_line(
                        "source: "
                        + str(item.get("source_id") or "")
                        + " "
                        + str(item.get("source_type") or "")
                        + " "
                        + str(item.get("title") or item.get("provider") or item.get("url") or "")
                    )
            elif command == "/resume":
                result = self.controller.apply_local_command(parsed)
                log.write_line(json.dumps(result, ensure_ascii=False))
            elif command == "/new":
                log.write_line(json.dumps(self.controller.apply_local_command(parsed), ensure_ascii=False))
            elif command == "/undo":
                plan = self.controller.apply_local_command(parsed)
                if plan.get("error"):
                    log.write_line(str(plan["error"]))
                    return
                data = await self.run_worker_thread(
                    lambda: self.api.request(
                        "POST",
                        f"/v1/sessions/{self.controller.session_id}/undo",
                        {"turns": plan.get("turns") or 1, "reason": "tui /undo"},
                        control=True,
                    )
                )
                log.write_line(json.dumps(data, ensure_ascii=False)[:2000])
            elif command == "/rollback":
                plan = self.controller.apply_local_command(parsed)
                if plan.get("error"):
                    log.write_line(str(plan["error"]))
                    return
                payload = {key: value for key, value in plan.items() if key in {"checkpoint_id", "path"}}
                payload["reason"] = "tui /rollback"
                data = await self.run_worker_thread(
                    lambda: self.api.request("POST", "/v1/hermes/admin/tools/agent_file_rollback", payload, control=True)
                )
                log.write_line(json.dumps(data, ensure_ascii=False)[:2000])
            elif command == "/stop":
                run_id = parts[1] if len(parts) > 1 else self.controller.current_run_id
                if not run_id:
                    log.write_line("no active run")
                    return
                data = await self.run_worker_thread(lambda: self.api.request("POST", f"/v1/runs/{run_id}/stop", {}, control=True))
                log.write_line(json.dumps(data, ensure_ascii=False)[:2000])
            elif command == "/steer":
                if len(parts) < 3:
                    log.write_line("usage: /steer <run_id> <instruction>")
                    return
                run_id = parts[1]
                instruction = parts[2]
                data = await self.run_worker_thread(lambda: self.api.request("POST", f"/v1/runs/{run_id}/steer", {"instruction": instruction}, control=True))
                log.write_line(json.dumps(data, ensure_ascii=False)[:2000])
            else:
                log.write_line(f"unknown command: {command}")

        async def render_timeline(self, run_id: str) -> None:
            log = self.query_one(Log)
            data = await self.run_worker_thread(lambda: self.api.request("GET", f"/v1/runs/{run_id}", control=True))
            payload = data.get("payload") or {}
            for event in list(payload.get("events") or [])[-20:]:
                reduced = self.controller.reduce_sse_event(event)
                log.write_line(f"[{reduced.get('event')}] {json.dumps(reduced.get('data') or {}, ensure_ascii=False)[:800]}")

        async def run_worker_thread(self, fn: Any) -> Any:
            import asyncio as _asyncio

            return await _asyncio.to_thread(fn)

        def action_stop_current(self) -> None:
            if self.controller.current_run_id:
                self.run_worker(self.handle_command(f"/stop {self.controller.current_run_id}"), exclusive=False)

        def action_new_session(self) -> None:
            self.controller.apply_local_command(TUISlashCommand(command="/new", raw="/new"))
            self.query_one(Log).write_line("new session")

    AIASKTUI().run()
