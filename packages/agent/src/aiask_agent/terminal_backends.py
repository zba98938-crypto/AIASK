from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .numeric import bounded_float, bounded_int
from .process_registry import ProcessRegistry


TERMINAL_BACKENDS = ("local", "docker", "ssh", "singularity", "modal", "daytona")


@dataclass(frozen=True)
class TerminalBackendStatus:
    name: str
    configured: bool
    available: bool
    persistent: bool
    reason: str | None = None
    driver: str | None = None
    supports_background: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "available": self.available,
            "persistent": self.persistent,
            "reason": self.reason,
            "driver": self.driver,
            "supports_background": self.supports_background,
        }


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _limit_bytes(value: bytes, limit: int) -> tuple[str, bool]:
    bounded = bounded_int(limit, default=65536, minimum=1, maximum=1024 * 1024)
    truncated = len(value) > bounded
    return value[:bounded].decode("utf-8", errors="replace"), truncated


def _env_with_allowlist(base: dict[str, str], allowlist: list[str] | tuple[str, ...] | None) -> dict[str, str]:
    env = dict(base)
    for key in list(allowlist or []):
        token = str(key or "").strip()
        if token and token in os.environ:
            env[token] = os.environ[token]
    return env


def _shell() -> str:
    if os.name == "nt":
        # Prefer PowerShell on Windows: it is POSIX-friendlier (pwd/ls/cat
        # aliases) and the only shell with a dedicated branch in
        # _shell_command_args. COMSPEC (cmd.exe) is kept as a last resort.
        # An operator can still force a specific shell via SHELL.
        return (
            os.environ.get("SHELL")
            or shutil.which("pwsh")
            or shutil.which("powershell")
            or os.environ.get("COMSPEC")
            or "powershell"
        )
    return os.environ.get("SHELL", "/bin/zsh")


def _shell_command_args(command: str) -> list[str]:
    shell = _shell()
    name = Path(shell).name.lower()
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    if name in {"cmd", "cmd.exe"}:
        return [shell, "/d", "/s", "/c", command]
    return [shell, "-lc", command]


def _direct_python_command_args(command: str) -> list[str] | None:
    """Turn ``<python> -c <code>`` into exec args directly.

    Routing ``python -c "print('x')"`` through PowerShell ``-Command`` mangles
    the inner quotes and yields empty stdout. We instead detect a python
    interpreter invocation (bare ``python``/``python3``/``py`` OR a full
    executable path whose stem starts with ``python``) followed by ``-c`` and
    a single code argument, and return direct exec args so no shell quoting is
    involved. Handles both single- and double-quoted code.
    """
    text = str(command or "").strip()
    if not text:
        return None
    current_python_prefix = f"{sys.executable} "
    if text.startswith(current_python_prefix):
        try:
            suffix = shlex.split(text[len(current_python_prefix):], posix=os.name != "nt")
        except ValueError:
            return None
        if len(suffix) >= 2 and suffix[0] == "-c":
            return [sys.executable, "-c", suffix[1]]
    # Use non-posix split so Windows backslash paths (F:\...\python.exe) are
    # preserved; posix mode would strip the backslashes. We then strip the
    # surrounding quotes from the code argument ourselves.
    try:
        tokens = shlex.split(text, posix=os.name != "nt")
    except ValueError:
        return None
    if len(tokens) < 3 or tokens[1] != "-c":
        return None
    interpreter = tokens[0].strip('"').strip("'")
    stem = Path(interpreter).stem.lower()
    is_python = stem in {"python", "python3", "py"} or stem.startswith("python")
    if not is_python:
        return None
    raw_code = tokens[2]
    if len(raw_code) >= 2 and raw_code[0] == raw_code[-1] and raw_code[0] in {"'", '"'}:
        code = raw_code[1:-1]
    else:
        code = raw_code
    exe = interpreter if (os.sep in interpreter or "/" in interpreter) else (shutil.which(interpreter) or interpreter)
    return [exe, "-c", code]


def _safe_name(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def command_targets_aiask_runtime(command: str) -> bool:
    raw = str(command or "").strip().lower()
    if not raw:
        return False
    own_pid = str(os.getpid())
    parent_pid = str(os.getppid())
    patterns = (
        r"\bpkill\b.*\baiask(?:-agent|_agent)?\b",
        r"\bkillall\b.*\baiask(?:-agent|_agent)?\b",
        r"\bkill\s+-\d+\s+" + re.escape(own_pid) + r"\b",
        r"\bkill\s+" + re.escape(own_pid) + r"\b",
        r"\bkill\s+-\d+\s+" + re.escape(parent_pid) + r"\b",
        r"\blaunchctl\b.*\baiask\b",
    )
    return any(re.search(pattern, raw) for pattern in patterns)


def backend_status(name: str) -> TerminalBackendStatus:
    backend = str(name or "local").strip().lower()
    if backend == "local":
        return TerminalBackendStatus("local", configured=True, available=True, persistent=False, driver="subprocess")
    if backend == "docker":
        docker = shutil.which("docker")
        sdk = _module_available("docker")
        reason = None if docker or sdk else "docker executable or docker SDK is not installed"
        return TerminalBackendStatus(
            "docker",
            configured=bool(docker or sdk),
            available=bool(docker or sdk),
            persistent=True,
            reason=reason,
            driver="docker_cli" if docker else "docker_sdk" if sdk else None,
        )
    if backend == "ssh":
        configured = bool(os.getenv("TERMINAL_SSH_HOST") and os.getenv("TERMINAL_SSH_USER"))
        asyncssh = _module_available("asyncssh")
        available = bool(asyncssh or shutil.which("ssh"))
        reason = None
        if not available:
            reason = "asyncssh package or ssh executable is required"
        elif not configured:
            reason = "TERMINAL_SSH_HOST and TERMINAL_SSH_USER are required"
        return TerminalBackendStatus("ssh", configured=configured, available=available, persistent=True, reason=reason, driver="asyncssh" if asyncssh else "ssh_cli")
    if backend == "singularity":
        exe = shutil.which("apptainer") or shutil.which("singularity")
        image = os.getenv("TERMINAL_SINGULARITY_IMAGE") or os.getenv("TERMINAL_APPTAINER_IMAGE")
        configured = bool(exe and image)
        reason = None
        if not exe:
            reason = "apptainer or singularity executable not found"
        elif not image:
            reason = "TERMINAL_SINGULARITY_IMAGE or TERMINAL_APPTAINER_IMAGE is required"
        return TerminalBackendStatus("singularity", configured=configured, available=bool(exe), persistent=True, reason=reason, driver=Path(exe).name if exe else None)
    if backend == "modal":
        direct = bool(
            (os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET"))
            or (Path.home() / ".modal.toml").exists()
            or os.getenv("AIASK_MODAL_TERMINAL_COMMAND")
        )
        available = _module_available("modal") or bool(shutil.which("modal")) or bool(os.getenv("AIASK_MODAL_TERMINAL_COMMAND"))
        reason = None
        if not available:
            reason = "modal package, modal CLI, or AIASK_MODAL_TERMINAL_COMMAND is required"
        elif not direct:
            reason = "Modal credentials are not configured"
        return TerminalBackendStatus("modal", configured=direct, available=available, persistent=True, reason=reason, driver="command" if os.getenv("AIASK_MODAL_TERMINAL_COMMAND") else "modal")
    if backend == "daytona":
        available = _module_available("daytona") or bool(shutil.which("daytona")) or bool(os.getenv("AIASK_DAYTONA_TERMINAL_COMMAND"))
        configured = bool(os.getenv("DAYTONA_API_KEY") or os.getenv("AIASK_DAYTONA_TERMINAL_COMMAND"))
        reason = None
        if not available:
            reason = "daytona package, daytona CLI, or AIASK_DAYTONA_TERMINAL_COMMAND is required"
        elif not configured:
            reason = "DAYTONA_API_KEY is not configured"
        return TerminalBackendStatus("daytona", configured=configured, available=available, persistent=True, reason=reason, driver="command" if os.getenv("AIASK_DAYTONA_TERMINAL_COMMAND") else "daytona")
    return TerminalBackendStatus(backend, configured=False, available=False, persistent=False, reason=f"unknown backend: {backend}")


def list_backends() -> list[dict[str, Any]]:
    return [backend_status(name).to_dict() for name in TERMINAL_BACKENDS]


def sessions(*, state_path: Path | None = None, limit: int = 200) -> list[dict[str, Any]]:
    items = ProcessRegistry(state_path).list(limit=limit)
    result: list[dict[str, Any]] = []
    for item in items:
        metadata = dict(item.get("metadata") or {})
        result.append(
            {
                "process_id": item.get("process_id"),
                "backend": metadata.get("backend") or "local",
                "session_id": item.get("session_id"),
                "status": item.get("status"),
                "cwd": item.get("cwd"),
                "command": item.get("command"),
                "started_at": metadata.get("started_at") or item.get("created_at"),
            }
        )
    return result


@dataclass(frozen=True)
class TerminalInvocation:
    command: str
    cwd: str
    backend: str = "local"
    session_id: str | None = None
    stdin: str | None = None
    background: bool = False
    pty: bool = False
    image: str | None = None
    resource_limits: dict[str, Any] | None = None
    timeout_seconds: float = 30.0
    max_output_bytes: int = 65536
    env: dict[str, str] | None = None
    notify_on_complete: bool = False


class TerminalBackendError(RuntimeError):
    def __init__(self, message: str, *, status: TerminalBackendStatus | None = None, code: str = "TERMINAL_BACKEND_ERROR") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class TerminalBackend:
    name = "local"

    def __init__(self, registry: ProcessRegistry) -> None:
        self.registry = registry

    def status(self) -> TerminalBackendStatus:
        return backend_status(self.name)

    def build_command(self, invocation: TerminalInvocation, process_id: str | None = None) -> tuple[list[str], dict[str, Any]]:
        direct = _direct_python_command_args(invocation.command)
        if direct is not None:
            return direct, {"direct_command": "python_c"}
        return _shell_command_args(invocation.command), {}

    def local_cwd(self, invocation: TerminalInvocation) -> str | None:
        return invocation.cwd


class DockerBackend(TerminalBackend):
    name = "docker"

    def build_command(self, invocation: TerminalInvocation, process_id: str | None = None) -> tuple[list[str], dict[str, Any]]:
        image = str(invocation.image or os.getenv("TERMINAL_DOCKER_IMAGE") or "").strip()
        if not image:
            raise TerminalBackendError("Docker backend requires image or TERMINAL_DOCKER_IMAGE", status=self.status(), code="TERMINAL_BACKEND_IMAGE_REQUIRED")
        container_name = _safe_name("aiask-terminal")
        args = ["docker", "run", "--rm", "-i", "--name", container_name, "-v", f"{invocation.cwd}:/workspace", "-w", "/workspace"]
        limits = dict(invocation.resource_limits or {})
        if limits.get("memory"):
            args.extend(["--memory", str(limits["memory"])])
        if limits.get("cpus"):
            args.extend(["--cpus", str(limits["cpus"])])
        args.extend([image, "/bin/sh", "-lc", invocation.command])
        return args, {"container_name": container_name, "image": image, "sandbox_id": container_name}


class SSHBackend(TerminalBackend):
    name = "ssh"

    def build_command(self, invocation: TerminalInvocation, process_id: str | None = None) -> tuple[list[str], dict[str, Any]]:
        host = str(os.getenv("TERMINAL_SSH_HOST") or "").strip()
        user = str(os.getenv("TERMINAL_SSH_USER") or "").strip()
        if not host or not user:
            raise TerminalBackendError("TERMINAL_SSH_HOST and TERMINAL_SSH_USER are required", status=self.status(), code="TERMINAL_BACKEND_NOT_CONFIGURED")
        port = str(os.getenv("TERMINAL_SSH_PORT") or "22").strip()
        key_path = str(os.getenv("TERMINAL_SSH_KEY_PATH") or "").strip()
        target = f"{user}@{host}"
        remote = invocation.command
        if invocation.cwd and invocation.cwd != ".":
            remote = f"cd {shlex.quote(invocation.cwd)} && {invocation.command}"
        args = ["ssh", "-p", port, "-o", "BatchMode=yes"]
        if key_path:
            args.extend(["-i", key_path])
        args.extend([target, remote])
        return args, {"host": host, "user": user, "port": port, "sandbox_id": target}

    def local_cwd(self, invocation: TerminalInvocation) -> str | None:
        return None


class SingularityBackend(TerminalBackend):
    name = "singularity"

    def build_command(self, invocation: TerminalInvocation, process_id: str | None = None) -> tuple[list[str], dict[str, Any]]:
        exe = shutil.which("apptainer") or shutil.which("singularity")
        image = str(invocation.image or os.getenv("TERMINAL_SINGULARITY_IMAGE") or os.getenv("TERMINAL_APPTAINER_IMAGE") or "").strip()
        if not exe:
            raise TerminalBackendError("apptainer or singularity executable not found", status=self.status(), code="TERMINAL_BACKEND_NOT_AVAILABLE")
        if not image:
            raise TerminalBackendError("Singularity backend requires image or TERMINAL_SINGULARITY_IMAGE", status=self.status(), code="TERMINAL_BACKEND_IMAGE_REQUIRED")
        args = [exe, "exec", "--pwd", invocation.cwd, image, "/bin/sh", "-lc", invocation.command]
        return args, {"image": image, "sandbox_id": image, "driver": Path(exe).name}


class WrapperCommandBackend(TerminalBackend):
    name = "modal"
    command_env = "AIASK_MODAL_TERMINAL_COMMAND"

    def build_command(self, invocation: TerminalInvocation, process_id: str | None = None) -> tuple[list[str], dict[str, Any]]:
        template = str(os.getenv(self.command_env) or "").strip()
        if not template:
            raise TerminalBackendError(
                f"{self.name} backend requires {self.command_env} for live command execution",
                status=self.status(),
                code="TERMINAL_BACKEND_RUNNER_REQUIRED",
            )
        rendered = template.format(
            command=shlex.quote(invocation.command),
            cwd=shlex.quote(invocation.cwd or "."),
            image=shlex.quote(str(invocation.image or "")),
        )
        # Prefer a direct python exec on Windows so quoted -c code is not
        # mangled by the PowerShell -Command path (mirrors the local backend).
        direct = _direct_python_command_args(rendered)
        if direct is not None:
            return direct, {"sandbox_id": self.name, "runner": self.command_env, "direct_command": "python_c"}
        return _shell_command_args(rendered), {"sandbox_id": self.name, "runner": self.command_env}

    def local_cwd(self, invocation: TerminalInvocation) -> str | None:
        return None


class ModalBackend(WrapperCommandBackend):
    name = "modal"
    command_env = "AIASK_MODAL_TERMINAL_COMMAND"


class DaytonaBackend(WrapperCommandBackend):
    name = "daytona"
    command_env = "AIASK_DAYTONA_TERMINAL_COMMAND"


class TerminalBackendManager:
    def __init__(self, registry: ProcessRegistry, *, allowed_roots: tuple[Path, ...] | None = None) -> None:
        self.registry = registry
        self.allowed_roots = allowed_roots
        self._attached: dict[str, subprocess.Popen[bytes]] = {}
        self._killed: set[str] = set()
        self.backends: dict[str, TerminalBackend] = {
            "local": TerminalBackend(registry),
            "docker": DockerBackend(registry),
            "ssh": SSHBackend(registry),
            "singularity": SingularityBackend(registry),
            "modal": ModalBackend(registry),
            "daytona": DaytonaBackend(registry),
        }

    def backend(self, name: str) -> TerminalBackend:
        token = str(name or "local").strip().lower() or "local"
        if token not in self.backends:
            raise TerminalBackendError(f"unknown backend: {token}", status=backend_status(token), code="TERMINAL_BACKEND_UNKNOWN")
        return self.backends[token]

    async def execute(self, invocation: TerminalInvocation) -> dict[str, Any]:
        backend = self.backend(invocation.backend)
        status = backend.status()
        if not status.available or not status.configured:
            raise TerminalBackendError(status.reason or f"terminal backend is not configured: {status.name}", status=status, code="TERMINAL_BACKEND_NOT_CONFIGURED")
        return await self._spawn(invocation, backend=backend) if invocation.background else await self._run(invocation, backend=backend)

    async def _run(self, invocation: TerminalInvocation, *, backend: TerminalBackend) -> dict[str, Any]:
        args, metadata = backend.build_command(invocation)
        status = backend.status()
        timeout = bounded_float(invocation.timeout_seconds, default=30.0, minimum=1.0, maximum=3600.0)
        max_output = bounded_int(invocation.max_output_bytes, default=65536, minimum=1, maximum=1024 * 1024)
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=backend.local_cwd(invocation),
            env=dict(invocation.env or os.environ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdin_bytes = None if invocation.stdin is None else str(invocation.stdin).encode("utf-8")
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            stdout, stderr = await proc.communicate()
        out_text, out_truncated = _limit_bytes(stdout or b"", max_output)
        err_text, err_truncated = _limit_bytes(stderr or b"", max_output)
        record = self.registry.record(
            command=invocation.command,
            cwd=invocation.cwd,
            status="timed_out" if timed_out else "completed",
            returncode=proc.returncode,
            stdout=out_text,
            stderr=err_text,
            session_id=invocation.session_id,
            metadata={
                **metadata,
                "backend": backend.name,
                "driver": backend.status().driver,
                "pty": bool(invocation.pty),
                "truncated": out_truncated or err_truncated,
                "foreground_args": args[:3],
            },
        )
        return {
            "process_id": record["process_id"],
            "command": invocation.command,
            "cwd": invocation.cwd,
            "returncode": proc.returncode,
            "stdout": out_text,
            "stderr": err_text,
            "timed_out": timed_out,
            "truncated": out_truncated or err_truncated,
            "backend": backend.name,
            "backend_status": status.to_dict(),
            "sandbox_id": metadata.get("sandbox_id"),
            "pty": bool(invocation.pty),
        }

    async def _spawn(self, invocation: TerminalInvocation, *, backend: TerminalBackend) -> dict[str, Any]:
        process_id = self.registry.new_process_id()
        stdout_path, stderr_path = self.registry.spool_paths(process_id)
        args, metadata = backend.build_command(invocation, process_id=process_id)
        max_output = bounded_int(invocation.max_output_bytes, default=65536, minimum=1, maximum=1024 * 1024)
        stdout_fh = stdout_path.open("ab")
        stderr_fh = stderr_path.open("ab")
        try:
            proc = subprocess.Popen(
                args,
                cwd=backend.local_cwd(invocation),
                env=dict(invocation.env or os.environ),
                stdin=subprocess.PIPE if invocation.stdin is not None else None,
                stdout=stdout_fh,
                stderr=stderr_fh,
            )
            if invocation.stdin is not None and proc.stdin is not None:
                proc.stdin.write(str(invocation.stdin).encode("utf-8"))
                proc.stdin.close()
        finally:
            stdout_fh.close()
            stderr_fh.close()
        record = self.registry.record(
            process_id=process_id,
            command=invocation.command,
            cwd=invocation.cwd,
            status="running",
            returncode=None,
            stdout="",
            stderr="",
            session_id=invocation.session_id,
            metadata={
                **metadata,
                "pid": proc.pid,
                "pid_scope": "local_wrapper",
                "backend": backend.name,
                "driver": backend.status().driver,
                "background": True,
                "aiask_managed": True,
                "attached": True,
                "started_at": int(time.time()),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "output_path": str(stdout_path),
                "notify_on_complete": bool(invocation.notify_on_complete),
            },
        )
        self._attached[record["process_id"]] = proc
        thread = threading.Thread(
            target=self._watch_subprocess,
            args=(record["process_id"], proc),
            kwargs={"max_output_bytes": max_output},
            daemon=True,
        )
        thread.start()
        return {
            "process_id": record["process_id"],
            "pid": proc.pid,
            "command": invocation.command,
            "cwd": invocation.cwd,
            "status": "running",
            "background": True,
            "attached": True,
            "backend": backend.name,
            "backend_status": backend.status().to_dict(),
            "sandbox_id": metadata.get("sandbox_id"),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }

    def _watch_subprocess(self, process_id: str, process: subprocess.Popen[bytes], *, max_output_bytes: int) -> None:
        try:
            returncode = process.wait()
            output = self.registry.read_output(process_id, max_bytes=max_output_bytes, tail=True)
            out_text = str(output.get("stdout") or "")
            err_text = str(output.get("stderr") or "")
            self.registry.update(
                process_id,
                status="killed" if process_id in self._killed else "completed",
                returncode=returncode,
                stdout=out_text,
                stderr=err_text,
                metadata={
                    "attached": False,
                    "last_seen_at": int(time.time()),
                    "truncated": len(out_text.encode("utf-8")) >= max_output_bytes or len(err_text.encode("utf-8")) >= max_output_bytes,
                },
            )
            if dict((self.registry.get(process_id) or {}).get("metadata") or {}).get("notify_on_complete"):
                asyncio.run(self._notify_complete(process_id))
        except Exception:
            return
        finally:
            self._attached.pop(process_id, None)
            self._killed.discard(process_id)

    async def _watch(self, process_id: str, process: asyncio.subprocess.Process, *, max_output_bytes: int) -> None:
        try:
            returncode = await process.wait()
            output = self.registry.read_output(process_id, max_bytes=max_output_bytes, tail=True)
            out_text = str(output.get("stdout") or "")
            err_text = str(output.get("stderr") or "")
            self.registry.update(
                process_id,
                status="killed" if process_id in self._killed else "completed",
                returncode=returncode,
                stdout=out_text,
                stderr=err_text,
                metadata={
                    "attached": False,
                    "last_seen_at": int(time.time()),
                    "truncated": len(out_text.encode("utf-8")) >= max_output_bytes or len(err_text.encode("utf-8")) >= max_output_bytes,
                },
            )
            if dict((self.registry.get(process_id) or {}).get("metadata") or {}).get("notify_on_complete"):
                await self._notify_complete(process_id)
        finally:
            self._attached.pop(process_id, None)
            self._killed.discard(process_id)

    async def _notify_complete(self, process_id: str) -> None:
        item = self.registry.get(process_id)
        if not item:
            return
        try:
            from .gateway import DeliveryRouter, GatewayConfigStore, GatewayMessageStore

            config = GatewayConfigStore()
            home = next((platform for platform in config.platforms() if platform.get("home_channel") and platform.get("enabled")), None)
            if not home:
                return
            await DeliveryRouter(config=config, messages=GatewayMessageStore(self.registry.path)).send(
                platform=str(home.get("name") or "local"),
                target=str(home.get("home_channel") or ""),
                message=f"AIASK background process {process_id} completed with status {item.get('status')}",
                session_id=item.get("session_id"),
            )
        except Exception:
            return

    async def process_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "list").strip().lower()
        if action == "list":
            return {"items": self.registry.list(session_id=arguments.get("session_id"), limit=bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=1000))}
        if action == "read":
            return self.registry.read_output(
                str(arguments.get("process_id") or "").strip(),
                max_bytes=bounded_int(arguments.get("max_output_bytes"), default=65536, minimum=1, maximum=1024 * 1024),
                tail=bool(arguments.get("tail", True)),
            )
        if action == "wait":
            return await self._wait(str(arguments.get("process_id") or "").strip(), timeout_seconds=bounded_float(arguments.get("timeout_seconds"), default=30.0, minimum=0.1, maximum=3600.0))
        if action == "watch":
            waited = await self._wait(str(arguments.get("process_id") or "").strip(), timeout_seconds=bounded_float(arguments.get("timeout_seconds"), default=1.0, minimum=0.1, maximum=3600.0))
            output = self.registry.read_output(
                str(arguments.get("process_id") or "").strip(),
                max_bytes=bounded_int(arguments.get("max_output_bytes"), default=65536, minimum=1, maximum=1024 * 1024),
                tail=bool(arguments.get("tail", True)),
            )
            return {"watch": True, **waited, "output": output}
        if action == "kill":
            return await self._kill(str(arguments.get("process_id") or "").strip())
        raise ValueError(f"unsupported process action: {action}")

    async def _wait(self, process_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        deadline = time.time() + bounded_float(timeout_seconds, default=30.0, minimum=0.1, maximum=3600.0)
        while time.time() < deadline:
            item = self.registry.refresh(process_id)
            if item is None:
                raise ValueError(f"process not found: {process_id}")
            if item.get("status") not in {"running", "detached_running"}:
                return {"completed": True, "process": item}
            await asyncio.sleep(0.1)
        return {"completed": False, "process": self.registry.get(process_id)}

    async def _kill(self, process_id: str) -> dict[str, Any]:
        item = self.registry.get(process_id)
        if item is None:
            return {"killed": False, "error": f"process not found: {process_id}"}
        metadata = dict(item.get("metadata") or {})
        backend = str(metadata.get("backend") or "local")
        if backend == "docker" and metadata.get("container_name") and shutil.which("docker"):
            subprocess.run(["docker", "rm", "-f", str(metadata["container_name"])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        proc = self._attached.get(process_id)
        if proc is not None and proc.returncode is None:
            self._killed.add(process_id)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            updated = self.registry.update(process_id, status="killed", returncode=proc.returncode, metadata={"attached": False})
            self._attached.pop(process_id, None)
            return {"killed": True, "process": updated}
        return self.registry.kill(process_id, allowed_roots=self.allowed_roots)
