#!/usr/bin/env python3
"""Start the factory supervisor detached from the caller's session.

The runbook starts the supervisor in the foreground, which is right for a terminal
an operator is watching. Started instead as a child of a task runner it dies with
its parent: an earlier attempt here was terminated by `SIGTERM` seven minutes in,
and the supervisor logged `received SIGTERM; stopping factories...` — a clean stop
of a healthy process, which then left nothing running and no defect to blame.

`setsid(1)` would do this on Linux and does not exist on macOS, so the detachment is
done with `os.setsid()` after a fork, which both platforms support, followed by
`execv` so the supervisor is the process being started rather than a grandchild of
a Python wrapper.

Usage::

    python scripts/start_factories_daemon.py                # standard arguments
    python scripts/start_factories_daemon.py --print-only   # show what it would run
    python scripts/stop_factories_daemon.py                 # see the sibling script

Any argument after the script name is passed through to the supervisor.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from aiask_quant_core.runtime_paths import runtime_home

REPO_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = REPO_ROOT / "scripts" / "factories" / "run_three_factories.py"
DEFAULT_LOG = runtime_home() / "logs" / "four_runtime_supervisor" / "supervisor.log"
DEFAULT_PID = runtime_home() / "run" / "aiask-supervisor.pid"

#: The runbook's arguments, with the interpreter named explicitly: the project runs
#: on the conda environment and `uv` is not used here.
DEFAULT_ARGS = (
    "--python", sys.executable,
    "--strategy-dispatch-run",
    "--strategy-dispatch-default-universe",
    "--strategy-dispatch-default-universe-limit", "200",
    "--incubation-run-time", "18:30",
)


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID))
    parser.add_argument("--print-only", action="store_true")
    parser.add_argument("supervisor_args", nargs="*")
    args = parser.parse_args(argv)

    extra = args.supervisor_args or list(DEFAULT_ARGS)
    command = [sys.executable, "-u", str(SUPERVISOR), *extra]

    if args.print_only:
        print(" ".join(command))
        return 0

    pid_file = Path(args.pid_file)
    Path(args.log).expanduser().parent.mkdir(parents=True, exist_ok=True)
    pid_file.expanduser().parent.mkdir(parents=True, exist_ok=True)
    if pid_file.exists():
        try:
            existing = int(pid_file.read_text().strip())
        except ValueError:
            existing = 0
        if existing and _running(existing):
            print(f"a supervisor is already running: pid={existing}; "
                  f"stop it first ({REPO_ROOT / 'scripts' / 'stop_factories_daemon.py'})")
            return 1
        pid_file.unlink(missing_ok=True)

    log_fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    pid = os.fork()
    if pid > 0:
        os.close(log_fd)
        # Wait for the child to either exec or die so the caller learns which.
        time.sleep(1.5)
        try:
            child = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            child = 0
        if child and _running(child):
            print(f"supervisor started detached: pid={child} log={args.log}")
            return 0
        print(f"supervisor did not survive startup; see {args.log}")
        return 1

    # Child: leave the caller's session and process group, then become the supervisor.
    os.setsid()
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    for fd in (log_fd, devnull):
        if fd > 2:
            os.close(fd)
    pid_file.write_text(str(os.getpid()))
    try:
        os.execv(command[0], command)
    except OSError as exc:  # pragma: no cover - exec failure is fatal and loud
        print(f"exec failed: {exc}", file=sys.stderr)
        os._exit(127)


if __name__ == "__main__":
    raise SystemExit(main())
