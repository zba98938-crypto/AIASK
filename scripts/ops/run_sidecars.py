#!/usr/bin/env python3
"""Supervise the three AIASK sidecars.

The four-runtime supervisor owns the factory runtimes; these sidecars are
separate services.  This process keeps them alive, restarts them after a
non-zero exit, and writes a status file for the health diagnostic.

  - FactorScheduler: daily factor_values / factor_ic_history refresh (18:00)
  - SignalTracker: daily signal cycle (18:30)
  - Agent HTTP: the AIASK Agent API on 127.0.0.1:8765
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_HOME = Path(
    os.getenv("AIASK_RUNTIME_HOME") or (Path.home() / ".aiask")
).expanduser()
LOG_DIR = RUNTIME_HOME / "logs" / "sidecars"
STATUS_PATH = RUNTIME_HOME / "run" / "sidecars.status.json"
PID_PATH = RUNTIME_HOME / "run" / "sidecars.pid"
AGENT_META_PATH = RUNTIME_HOME / "run" / "agent.meta.json"
AGENT_PID_PATH = RUNTIME_HOME / "run" / "agent.pid"
RESTART_DELAY_SEC = 30


@dataclass(frozen=True)
class SidecarSpec:
    name: str
    command: tuple[str, ...]
    log_name: str


@dataclass
class SidecarState:
    spec: SidecarSpec
    process: subprocess.Popen | None = None
    started_at: float | None = None
    restart_count: int = 0
    last_exit_code: int | None = None
    next_restart_at: float | None = None
    status: str = "pending"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sqlite_path() -> str:
    explicit = str(os.getenv("AIASK_SQLITE_PATH") or "").strip()
    if explicit:
        return explicit
    return str(RUNTIME_HOME / "data" / "db" / "akshare_mcp.sqlite3")


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("AIASK_RUNTIME_HOME", str(RUNTIME_HOME))
    env.setdefault("AIASK_PROJECT_ROOT", str(ROOT))
    env.setdefault("AIASK_SQLITE_PATH", _sqlite_path())
    env.setdefault("AKSHARE_MCP_SQLITE_PATH", env["AIASK_SQLITE_PATH"])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _specs() -> list[SidecarSpec]:
    python = sys.executable
    return [
        SidecarSpec(
            "factor_scheduler",
            (python, "-u", str(ROOT / "scripts/factories/run_factor_scheduler.py")),
            "factor_scheduler.log",
        ),
        SidecarSpec(
            "signal_tracker",
            (
                python,
                "-u",
                str(ROOT / "scripts/factories/run_signal_tracker.py"),
                "--daemon",
                "--run-time",
                "18:30",
            ),
            "signal_tracker.log",
        ),
        SidecarSpec(
            "agent_http",
            (
                python,
                "-m",
                "aiask_agent.server",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
            ),
            "agent_http.log",
        ),
    ]


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _write_agent_meta(state: SidecarState) -> None:
    if state.process is None:
        return
    command = list(state.spec.command)
    _write_json_atomic(
        AGENT_META_PATH,
        {
            "aiask_root": str(ROOT),
            "base_url": "http://127.0.0.1:8765",
            "command": command,
            "host": "127.0.0.1",
            "pid": state.process.pid,
            "port": 8765,
            "venv_python": sys.executable,
        },
    )
    AGENT_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PID_PATH.write_text(f"{state.process.pid}\n", encoding="utf-8")


def _write_status(states: list[SidecarState], *, supervisor_status: str) -> None:
    payload = {
        "name": "sidecar_supervisor",
        "status": supervisor_status,
        "pid": os.getpid(),
        "updated_at": _now_iso(),
        "sidecars": [
            {
                "name": state.spec.name,
                "status": state.status,
                "pid": state.process.pid if state.process else None,
                "started_at": (
                    datetime.fromtimestamp(state.started_at).astimezone().isoformat(
                        timespec="seconds"
                    )
                    if state.started_at
                    else None
                ),
                "restart_count": state.restart_count,
                "last_exit_code": state.last_exit_code,
                "next_restart_at": (
                    datetime.fromtimestamp(state.next_restart_at).astimezone().isoformat(
                        timespec="seconds"
                    )
                    if state.next_restart_at
                    else None
                ),
                "command": list(state.spec.command),
            }
            for state in states
        ],
    }
    _write_json_atomic(STATUS_PATH, payload)


def _start(state: SidecarState, *, restarting: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / state.spec.log_name
    log = open(log_path, "ab")
    try:
        state.process = subprocess.Popen(
            list(state.spec.command),
            cwd=ROOT,
            env=_child_env(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    state.started_at = time.time()
    state.next_restart_at = None
    state.status = "running"
    if restarting:
        state.restart_count += 1
    if state.spec.name == "agent_http":
        _write_agent_meta(state)


def _stop(state: SidecarState, *, grace_sec: float = 10.0) -> None:
    process = state.process
    if process is None or process.poll() is not None:
        state.status = "stopped"
        state.process = None
        return
    process.terminate()
    deadline = time.time() + grace_sec
    while time.time() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.2)
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)
    state.last_exit_code = process.returncode
    state.status = "stopped"
    state.process = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PID_PATH.is_file():
        try:
            existing = int(PID_PATH.read_text(encoding="utf-8").strip())
        except ValueError:
            existing = 0
        if existing and _pid_alive(existing):
            print(f"sidecar supervisor already running: pid={existing}", file=sys.stderr)
            return 1
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")

    stop_requested = False

    def _request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _request_stop)

    states = [SidecarState(spec) for spec in _specs()]
    for state in states:
        _start(state, restarting=False)
    _write_status(states, supervisor_status="running")
    print(
        f"sidecar supervisor started pid={os.getpid()} "
        f"status={STATUS_PATH}",
        flush=True,
    )

    try:
        while not stop_requested:
            now = time.time()
            for state in states:
                process = state.process
                if process is not None:
                    returncode = process.poll()
                    if returncode is None:
                        continue
                    state.last_exit_code = returncode
                    state.process = None
                    state.status = "exited"
                    state.next_restart_at = now + RESTART_DELAY_SEC
                    print(
                        f"{state.spec.name} exited code={returncode}; "
                        f"restart in {RESTART_DELAY_SEC}s",
                        flush=True,
                    )
                elif state.next_restart_at is not None and now >= state.next_restart_at:
                    _start(state, restarting=True)
                    print(
                        f"{state.spec.name} restarted pid={state.process.pid if state.process else None}",
                        flush=True,
                    )
            _write_status(states, supervisor_status="running")
            time.sleep(5)
    finally:
        for state in states:
            _stop(state)
        _write_status(states, supervisor_status="stopped")
        try:
            PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
