"""Small process/heartbeat checks shared by runtime status readers."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any


def pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def effective_status(
    reported_status: str,
    *,
    pid: Any = None,
    heartbeat_at: Any = None,
    heartbeat_period_seconds: int = 60,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return a fail-closed status and preserve the writer's reported status.

    A known dead process wins over a stale heartbeat. A heartbeat is stale
    after three expected update periods; a missing heartbeat does not infer
    liveness, so a reported running status with no process is dead.
    """
    reported = str(reported_status or "unknown").strip().lower()
    if reported == "running" and pid is not None and not pid_alive(pid):
        return {"effective_status": "dead", "last_reported_status": reported}
    if heartbeat_at:
        try:
            stamp = datetime.fromisoformat(str(heartbeat_at).replace("Z", "+00:00"))
            current = now or datetime.now(stamp.tzinfo)
            if current - stamp > timedelta(seconds=max(1, heartbeat_period_seconds) * 3):
                return {"effective_status": "stale", "last_reported_status": reported}
        except (TypeError, ValueError):
            return {"effective_status": "stale", "last_reported_status": reported}
    if reported == "running" and pid is None and not heartbeat_at:
        return {"effective_status": "dead", "last_reported_status": reported}
    return {"effective_status": reported, "last_reported_status": reported}


__all__ = ["effective_status", "pid_alive"]
