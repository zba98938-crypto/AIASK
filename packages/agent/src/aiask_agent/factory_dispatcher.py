"""ActionIntent-gated persistent dispatcher for the four factory workers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiask_quant_core.storage import get_db
from strategy_factory.api import normalize_factory_name

from .intents import ActionIntentStore


class FactoryDispatchRejected(PermissionError):
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

    env = os.environ.copy()
    command = [sys.executable, "-m", "aiask_agent.factory_worker", "--run-id", str(run["run_id"])]
    process = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        close_fds=os.name != "nt",
    )
    return {
        "success": True,
        "data": {
            "run": run,
            "worker_started": True,
            "worker_pid": process.pid,
        },
        "error": None,
    }


__all__ = ["FactoryDispatchRejected", "dispatch_factory_run"]
