#!/usr/bin/env python3
"""Stop the detached factory supervisor started by `start_factories_daemon.py`.

Signals the supervisor's process group, because the supervisor and its four children
form one and a bare `kill` on the supervisor alone leaves the children orphaned —
which is how a "stopped" factory keeps writing.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

PID_FILE = Path("/tmp/aiask-supervisor.pid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pid-file", default=str(PID_FILE))
    args = parser.parse_args(argv)

    path = Path(args.pid_file)
    if not path.exists():
        print("no pid file; nothing started by the daemon script is running")
        return 0
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        print(f"pid file is unreadable: {path}")
        return 1

    try:
        pgid = os.getpgid(pid)
    except OSError:
        print(f"pid {pid} is not running; removing the stale pid file")
        path.unlink(missing_ok=True)
        return 0

    print(f"stopping process group {pgid} (supervisor pid {pid})")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError as exc:
        print(f"could not signal the group: {exc}")
        return 1
    for _ in range(20):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    path.unlink(missing_ok=True)
    print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
