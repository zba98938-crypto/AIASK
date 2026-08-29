"""ActionIntent-gated persistent dispatcher for the four factory workers."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiask_quant_core.storage import get_db
from strategy_factory.api import normalize_factory_name

from .intents import ActionIntentStore


# How long to wait for an immediate startup crash before treating the worker as
# launched. Import failures surface well inside this window.
_STARTUP_GRACE_SECONDS = 2.0
_STDERR_TAIL_CHARS = 4096


class FactoryDispatchRejected(PermissionError):
    pass


def _worker_env() -> dict[str, str]:
    """Environment for the worker subprocess, with importability guaranteed.

    ``python -m aiask_agent.factory_worker`` only resolves if the package is on
    the child's import path. When the packages are consumed from source (the
    pytest.ini ``pythonpath`` layout, or any editable/source checkout) the parent
    imports fine while a bare ``os.environ.copy()`` child does not, so the worker
    died instantly with ModuleNotFoundError. Propagate the parent's resolved
    package roots explicitly.
    """
    env = os.environ.copy()
    existing = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]
    roots: list[str] = []
    for module in ("aiask_agent", "aiask_quant_core", "strategy_factory", "akshare_mcp"):
        loaded = sys.modules.get(module)
        origin = getattr(loaded, "__file__", None) if loaded is not None else None
        if not origin:
            continue
        # <root>/<package>/__init__.py -> <root>
        root = str(Path(origin).resolve().parent.parent)
        if root not in roots:
            roots.append(root)
    combined = [*existing, *[root for root in roots if root not in existing]]
    if combined:
        env["PYTHONPATH"] = os.pathsep.join(combined)
    return env


def _startup_failure(process: subprocess.Popen[bytes]) -> tuple[int, str] | None:
    """Return (exit_code, stderr_tail) when the worker died during startup.

    Returns None when the worker is still alive after the grace period, which is
    the normal case for a run that is actually executing.
    """
    try:
        stderr_bytes = process.communicate(timeout=_STARTUP_GRACE_SECONDS)[1] or b""
    except subprocess.TimeoutExpired:
        return None
    exit_code = int(process.returncode or 0)
    if exit_code == 0:
        # Exited cleanly and fast: the worker completed or found nothing to do
        # (e.g. the run was already claimed). Not a launch failure.
        return None
    tail = stderr_bytes.decode("utf-8", errors="replace")[-_STDERR_TAIL_CHARS:]
    return exit_code, tail


async def _mark_launch_failed(
    db: Any,
    run_id: str,
    *,
    command: list[str],
    started_at: str,
    failure_kind: str,
    error: str,
    stderr_tail: str | None,
    exit_code: int | None = None,
) -> None:
    """Record a launch failure so the run fails visibly instead of hanging."""
    await db.record_factory_worker_launch(
        run_id,
        worker_started_at=started_at,
        worker_command=" ".join(command),
    )
    current = await db.get_factory_run(run_id)
    if current is not None and current.get("terminal"):
        # The worker got far enough to record its own outcome before dying;
        # that verdict is more precise than ours, so do not overwrite it.
        return
    try:
        await db.transition_factory_run(
            run_id,
            "failed",
            phase="worker_launch_failed",
            failure_kind=failure_kind,
            stderr_tail=stderr_tail,
            exit_code=exit_code,
            error=error,
        )
    except ValueError:
        # Lost a race into a terminal state. The launch diagnostics above are
        # already persisted, so the failure is still traceable.
        pass


def _verified_intent(intent_id: str, intent_db_path: str) -> dict[str, Any]:
    if not intent_id or not intent_db_path:
        raise FactoryDispatchRejected("factory dispatch requires an executing ActionIntent")
    intent = ActionIntentStore(Path(intent_db_path)).get(intent_id)
    if intent is None:
        raise FactoryDispatchRejected("factory dispatch ActionIntent was not found")
    if str(intent.get("status") or "") != "executing":
        raise FactoryDispatchRejected("factory dispatch ActionIntent is not executing")
    if (str(intent.get("target_tool") or ""), str(intent.get("target_action") or "")) != (
        "factory_control",
        "run",
    ):
        raise FactoryDispatchRejected("factory dispatch ActionIntent scope mismatch")
    return intent


async def dispatch_factory_run(params: dict[str, Any]) -> dict[str, Any]:
    payload = dict(params or {})
    intent_id = str(payload.pop("_intent_id", "") or "").strip()
    intent_db_path = str(payload.pop("_intent_db_path", "") or "").strip()
    _verified_intent(intent_id, intent_db_path)

    factory = normalize_factory_name(payload.get("factory")).value
    idempotency_key = str(payload.get("idempotency_key") or intent_id).strip()
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    run_params = dict(payload.get("params") or {})
    db = get_db()
    await db.initialize()
    run = await db.request_factory_run(
        {
            "factory": factory,
            "idempotency_key": idempotency_key,
            "request_id": intent_id,
            "mode": str(payload.get("mode") or "once"),
            "params": run_params,
        }
    )
    if run.get("deduplicated") and run.get("status") != "queued":
        return {
            "success": True,
            "data": {"run": run, "worker_started": False},
            "error": None,
        }

    run_id = str(run["run_id"])
    env = _worker_env()
    command = [sys.executable, "-m", "aiask_agent.factory_worker", "--run-id", run_id]
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            # stderr used to go to DEVNULL, so a worker that died during import
            # (e.g. ModuleNotFoundError) left no trace at all and the run sat in
            # `queued` forever. Keep a pipe so startup failures are diagnosable.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            close_fds=os.name != "nt",
        )
    except OSError as exc:
        await _mark_launch_failed(
            db,
            run_id,
            command=command,
            started_at=started_at,
            failure_kind="worker_spawn_failed",
            error=f"could not spawn factory worker: {exc}",
            stderr_tail=None,
        )
        return {
            "success": False,
            "data": {"run": await db.get_factory_run(run_id), "worker_started": False},
            "error": f"could not spawn factory worker: {exc}",
            "error_code": "FACTORY_WORKER_SPAWN_FAILED",
        }

    # A worker that fails on import dies within milliseconds. Give it a short
    # grace period so that failure is reported here instead of silently leaving
    # the run queued until the stale-recovery sweep notices much later.
    startup_error = await asyncio.to_thread(_startup_failure, process)
    if startup_error is not None:
        exit_code, stderr_tail = startup_error
        await _mark_launch_failed(
            db,
            run_id,
            command=command,
            started_at=started_at,
            failure_kind="worker_start_failed",
            error=f"factory worker exited during startup with code {exit_code}",
            stderr_tail=stderr_tail,
            exit_code=exit_code,
        )
        return {
            "success": False,
            "data": {
                "run": await db.get_factory_run(run_id),
                "worker_started": False,
                "worker_exit_code": exit_code,
                "worker_stderr_tail": stderr_tail,
            },
            "error": f"factory worker exited during startup with code {exit_code}",
            "error_code": "FACTORY_WORKER_START_FAILED",
        }

    # Diagnostics only: the worker owns the state machine and may already have
    # driven this run to a terminal state while we were watching for a startup
    # crash, so this must not attempt a status transition.
    await db.record_factory_worker_launch(
        run_id,
        worker_started_at=started_at,
        worker_command=" ".join(command),
        pid=process.pid,
    )
    return {
        "success": True,
        "data": {
            "run": await db.get_factory_run(run_id) or run,
            "worker_started": True,
            "worker_pid": process.pid,
        },
        "error": None,
    }


__all__ = ["FactoryDispatchRejected", "dispatch_factory_run"]
