"""Scheduler handlers for quant_manager."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4


_RUN_NOW_TASK: asyncio.Task | None = None
_RUN_NOW_THREAD: threading.Thread | None = None
_RUN_NOW_JOB: dict[str, Any] | None = None
_RUN_NOW_LOCK = threading.RLock()


def _job_update(**updates: Any) -> dict[str, Any]:
    global _RUN_NOW_JOB
    with _RUN_NOW_LOCK:
        current = dict(_RUN_NOW_JOB or {})
        current.update(updates)
        _RUN_NOW_JOB = current
        return dict(current)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _job_snapshot() -> dict[str, Any] | None:
    with _RUN_NOW_LOCK:
        if _RUN_NOW_JOB is None:
            return None
        job = dict(_RUN_NOW_JOB)
    task = _RUN_NOW_TASK
    thread = _RUN_NOW_THREAD
    if task is not None:
        job["task_done"] = bool(task.done())
    if thread is not None:
        job["thread_alive"] = bool(thread.is_alive())
    return job


def _result_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"message": "run completed"}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    return {
        "run_id": result.get("run_id") or summary.get("run_id"),
        "status": result.get("status") or summary.get("status"),
        "computed": int(result.get("computed") or summary.get("computed") or 0),
        "errors": int(result.get("errors") or summary.get("errors") or 0),
        "elapsed_seconds": result.get("elapsed_seconds") or summary.get("elapsed_seconds"),
        "quality_status": result.get("quality_status") or summary.get("quality_status"),
    }


async def _run_scheduler_job(*, scheduler: Any, job_id: str) -> None:
    started_at = datetime.now().isoformat()
    _job_update(job_id=job_id, status="running", started_at=started_at, completed_at=None)
    perf_start = time.perf_counter()
    try:
        result = await scheduler.run_once()
        _job_update(
            status="completed",
            completed_at=datetime.now().isoformat(),
            elapsed_seconds=round(time.perf_counter() - perf_start, 3),
            result_summary=_result_summary(result),
            error=None,
        )
    except asyncio.CancelledError:
        _job_update(
            status="cancelled",
            completed_at=datetime.now().isoformat(),
            elapsed_seconds=round(time.perf_counter() - perf_start, 3),
            error="cancelled",
        )
        raise
    except Exception as exc:
        _job_update(
            status="failed",
            completed_at=datetime.now().isoformat(),
            elapsed_seconds=round(time.perf_counter() - perf_start, 3),
            error=str(exc),
        )


def _apply_scheduler_overrides(scheduler: Any, params: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    codes = params.get("codes") or params.get("universe")
    if isinstance(codes, str):
        try:
            import json

            parsed = json.loads(codes)
            codes = parsed if isinstance(parsed, list) else codes
        except Exception:
            codes = [item.strip() for item in codes.split(",") if item.strip()]
    if isinstance(codes, list):
        clean_codes = [str(item).strip() for item in codes if str(item).strip()]
        if clean_codes:
            scheduler.universe = clean_codes
            scheduler._skip_dynamic_universe = True
            overrides["universe_size"] = len(clean_codes)

    factors = params.get("factors")
    if isinstance(factors, str):
        try:
            import json

            parsed = json.loads(factors)
            factors = parsed if isinstance(parsed, list) else factors
        except Exception:
            factors = [item.strip() for item in factors.split(",") if item.strip()]
    if isinstance(factors, list):
        clean_factors = [str(item).strip() for item in factors if str(item).strip()]
        if clean_factors:
            scheduler.factors = clean_factors
            overrides["factors"] = list(clean_factors)

    if params.get("batch_size") is not None:
        try:
            scheduler.batch_size = max(1, int(params.get("batch_size") or 1))
            overrides["batch_size"] = scheduler.batch_size
        except Exception:
            pass

    if params.get("max_codes") is not None and not overrides.get("universe_size"):
        try:
            max_codes = max(1, int(params.get("max_codes") or 1))
            scheduler.universe = list(scheduler.universe or [])[:max_codes]
            scheduler._skip_dynamic_universe = True
            overrides["universe_size"] = len(scheduler.universe)
            overrides["max_codes"] = max_codes
        except Exception:
            pass
    return overrides


def _run_scheduler_thread(*, job_id: str, params: dict[str, Any]) -> None:
    try:
        async def _main() -> None:
            from ...services.factor_scheduler import FactorScheduler
            from ...storage import close_db

            scheduler = FactorScheduler()
            overrides = _apply_scheduler_overrides(scheduler, params)
            if overrides:
                _job_update(overrides=overrides)
            try:
                await _run_scheduler_job(scheduler=scheduler, job_id=job_id)
            finally:
                try:
                    await close_db()
                except Exception:
                    pass

        asyncio.run(_main())
    except Exception as exc:
        _job_update(
            job_id=job_id,
            status="failed",
            completed_at=datetime.now().isoformat(),
            error=str(exc),
        )


async def handle_scheduler_status(*, ok: Callable[..., dict]) -> dict:
    from ...services.factor_scheduler import get_factor_scheduler

    scheduler = get_factor_scheduler()
    status = dict(scheduler.status() or {})
    status["run_now_job"] = _job_snapshot()
    return ok(status)


async def handle_scheduler_run_now(*, kw: dict[str, Any] | None = None, ok: Callable[..., dict]) -> dict:
    global _RUN_NOW_TASK, _RUN_NOW_THREAD, _RUN_NOW_JOB
    from ...services.factor_scheduler import get_factor_scheduler

    params = dict(kw or {})
    scheduler = get_factor_scheduler()
    dry_run = _coerce_bool(params.get("dry_run"), False)
    wait = _coerce_bool(params.get("wait"), False)
    run_async = _coerce_bool(params.get("run_async"), True)
    if dry_run:
        return ok(
            {
                "status": "dry_run",
                "would_schedule": True,
                "run_async_default": True,
                "scheduler_status": scheduler.status(),
                "run_now_job": _job_snapshot(),
            }
        )

    if (
        (_RUN_NOW_TASK is not None and not _RUN_NOW_TASK.done())
        or (_RUN_NOW_THREAD is not None and _RUN_NOW_THREAD.is_alive())
    ):
        return ok(
            {
                "status": "already_running",
                "job_id": (_RUN_NOW_JOB or {}).get("job_id"),
                "run_now_job": _job_snapshot(),
                "scheduler_status": scheduler.status(),
            }
        )

    job_id = str(params.get("job_id") or f"quant_scheduler_run_now_{int(time.time())}_{uuid4().hex[:8]}")
    if not run_async:
        _apply_scheduler_overrides(scheduler, params)
        perf_start = time.perf_counter()
        result = await scheduler.run_once()
        return ok(
            {
                "status": "completed",
                "job_id": job_id,
                "elapsed_seconds": round(time.perf_counter() - perf_start, 3),
                "result_summary": _result_summary(result),
                "result": result or {"message": "run completed"},
            }
        )

    _RUN_NOW_TASK = None
    _job_update(
        job_id=job_id,
        status="queued",
        started_at=None,
        completed_at=None,
        elapsed_seconds=None,
        result_summary=None,
        error=None,
    )
    _RUN_NOW_THREAD = threading.Thread(
        target=_run_scheduler_thread,
        kwargs={"job_id": job_id, "params": params},
        name=f"quant-scheduler-run-now:{job_id}",
        daemon=True,
    )
    _RUN_NOW_THREAD.start()

    if wait:
        timeout_sec = max(0.1, min(float(params.get("timeout_sec", 5) or 5), 30.0))
        deadline = time.perf_counter() + timeout_sec
        while time.perf_counter() < deadline and _RUN_NOW_THREAD.is_alive():
            await asyncio.sleep(0.05)
        if _RUN_NOW_THREAD.is_alive():
            return ok(
                {
                    "status": "scheduled",
                    "job_id": job_id,
                    "wait_timeout_sec": timeout_sec,
                    "run_now_job": _job_snapshot(),
                    "scheduler_status": scheduler.status(),
                }
            )

    return ok(
        {
            "status": (
                "scheduled"
                if str((_RUN_NOW_JOB or {}).get("status") or "").lower() == "queued"
                else ((_RUN_NOW_JOB or {}).get("status") or "scheduled")
            ),
            "job_id": job_id,
            "run_now_job": _job_snapshot(),
            "scheduler_status": scheduler.status(),
        }
    )
