"""Online model retrain scheduler with lightweight lease governance."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from .artifact_registry import get_artifact_async, list_artifacts_async, register_artifact_async

logger = logging.getLogger(__name__)

_RETRAIN_PLAN_STRATEGY = "quant_model_retrain_plan"
_DEFAULT_POLL_SEC = 300
_DEFAULT_LEASE_TTL_SEC = 900
_DEFAULT_MAX_PLANS_PER_RUN = 1

_QUANT_MANAGER_IMPL = None
_scheduler: Optional["ModelRetrainScheduler"] = None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _safe_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _parse_datetime_like(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for parser in (
        datetime.fromisoformat,
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")),
        lambda item: datetime.strptime(item, "%Y-%m-%d %H:%M:%S"),
        lambda item: datetime.strptime(item, "%Y-%m-%d"),
    ):
        try:
            parsed = parser(raw)
            return parsed.astimezone() if parsed.tzinfo is not None else parsed.astimezone()
        except Exception:
            continue
    return None


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat()


def _payload_from_artifact_row(artifact: dict[str, Any]) -> dict[str, Any]:
    payload = artifact.get("payload")
    if isinstance(payload, dict):
        return deepcopy(payload)
    return {}


def _probe_manager(register_fn):
    class _Probe:
        def __init__(self):
            self.impl = None

        def tool(self):
            def _decorator(fn):
                self.impl = fn
                return fn

            return _decorator

    probe = _Probe()
    register_fn(probe)
    return probe.impl


def _get_quant_manager_impl():
    global _QUANT_MANAGER_IMPL
    if _QUANT_MANAGER_IMPL is None:
        from ..tools.managers.quant_manager import register_quant_manager

        _QUANT_MANAGER_IMPL = _probe_manager(register_quant_manager)
    return _QUANT_MANAGER_IMPL


async def _default_execute_plan(plan_id: str, plan_payload: dict[str, Any], *, reason: str) -> dict[str, Any]:
    impl = _get_quant_manager_impl()
    if impl is None:
        raise RuntimeError("quant_manager implementation unavailable")
    return await impl(
        action="model_registry",
        kwargs={
            "op": "execute_retrain",
            "artifact_id": plan_id,
            "execution_mode": "scheduled",
            "scheduler_reason": reason,
            "codes": list(plan_payload.get("codes") or []),
        },
    )


class ModelRetrainScheduler:
    """Async polling scheduler for retrain plan governance and execution."""

    def __init__(
        self,
        *,
        poll_interval_sec: int | None = None,
        lease_ttl_sec: int | None = None,
        max_plans_per_run: int | None = None,
        executor: Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]] | None = None,
    ):
        env_poll = int(str(os.getenv("MODEL_RETRAIN_SCHEDULER_POLL_SEC", poll_interval_sec or _DEFAULT_POLL_SEC)).strip() or _DEFAULT_POLL_SEC)
        env_lease = int(str(os.getenv("MODEL_RETRAIN_SCHEDULER_LEASE_TTL_SEC", lease_ttl_sec or _DEFAULT_LEASE_TTL_SEC)).strip() or _DEFAULT_LEASE_TTL_SEC)
        env_max = int(str(os.getenv("MODEL_RETRAIN_SCHEDULER_MAX_PLANS", max_plans_per_run or _DEFAULT_MAX_PLANS_PER_RUN)).strip() or _DEFAULT_MAX_PLANS_PER_RUN)

        self.poll_interval_sec = max(30, env_poll)
        self.lease_ttl_sec = max(60, env_lease)
        self.max_plans_per_run = max(1, env_max)
        self.instance_id = _safe_text(os.getenv("MODEL_RETRAIN_SCHEDULER_INSTANCE_ID")) or f"model-retrain-scheduler:{os.getpid()}"
        self._executor = executor
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._run_lock = asyncio.Lock()
        self.last_scan_at: datetime | None = None
        self.last_result: dict[str, Any] | None = None
        self.current_plan_id: str | None = None

    def start(self) -> None:
        if self._running:
            logger.warning("ModelRetrainScheduler already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="model-retrain-scheduler")
        logger.info("ModelRetrainScheduler started: poll=%ss lease_ttl=%ss", self.poll_interval_sec, self.lease_ttl_sec)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("ModelRetrainScheduler stopped")

    async def shutdown(self, grace_sec: float = 3.0) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            logger.info("ModelRetrainScheduler stopped")
            return
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, grace_sec))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        else:
            with suppress(asyncio.CancelledError):
                await task
        logger.info("ModelRetrainScheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once(reason="scheduled_loop")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ModelRetrainScheduler loop error: %s", exc, exc_info=True)
            try:
                await asyncio.sleep(self.poll_interval_sec)
            except asyncio.CancelledError:
                break

    async def _list_plan_artifacts(self, *, limit: int = 200) -> list[dict[str, Any]]:
        fetch_limit = max(50, min(1000, int(limit)))
        rows = await list_artifacts_async(limit=fetch_limit)
        artifacts = []
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("strategy") or "").strip().lower() != _RETRAIN_PLAN_STRATEGY:
                continue
            artifact_id = _safe_text(row.get("artifact_id"))
            if not artifact_id:
                continue
            artifact = await get_artifact_async(artifact_id)
            if artifact is not None:
                artifacts.append(artifact)
        return artifacts

    @staticmethod
    def _normalize_schedule_hint(payload: dict[str, Any]) -> str:
        return (_safe_text(payload.get("schedule_hint")) or "manual_review").lower()

    @classmethod
    def _schedule_enabled(cls, payload: dict[str, Any], *, force: bool = False) -> bool:
        if force:
            return True
        hint = cls._normalize_schedule_hint(payload)
        execution_mode = (_safe_text(payload.get("execution_mode")) or "plan_only").lower()
        if execution_mode in {"auto", "scheduled"}:
            return True
        return hint not in {"manual_review", "manual", "on_demand", "adhoc"}

    @staticmethod
    def _schedule_delta(payload: dict[str, Any]) -> timedelta | None:
        hint = (_safe_text(payload.get("schedule_hint")) or "").lower()
        interval_hours = payload.get("schedule_interval_hours")
        if interval_hours not in (None, ""):
            try:
                return timedelta(hours=max(1, int(interval_hours)))
            except Exception:
                pass
        if hint in {"hourly", "intraday_hourly"}:
            return timedelta(hours=1)
        if hint in {"intraday", "recheck_intraday"}:
            return timedelta(hours=4)
        if hint in {"daily", "trading_day"}:
            return timedelta(days=1)
        if hint in {"weekly", "week"}:
            return timedelta(days=7)
        if hint in {"monthly", "month"}:
            return timedelta(days=30)
        return None

    @classmethod
    def _resolve_next_run_dt(cls, payload: dict[str, Any], *, now: datetime | None = None) -> datetime | None:
        current = now or datetime.now().astimezone()
        explicit_next = _parse_datetime_like(payload.get("next_run_at"))
        if explicit_next is not None:
            return explicit_next
        run_count = int(payload.get("run_count", 0) or 0)
        hint = cls._normalize_schedule_hint(payload)
        if hint in {"auto", "immediate", "once"} and run_count <= 0:
            return current
        delta = cls._schedule_delta(payload)
        if delta is None:
            return None
        anchor = (
            _parse_datetime_like(payload.get("last_finished_at"))
            or _parse_datetime_like(payload.get("last_run_at"))
            or _parse_datetime_like(payload.get("created_at"))
            or current
        )
        if run_count <= 0 and payload.get("last_finished_at") in (None, "") and payload.get("last_run_at") in (None, ""):
            return anchor
        return anchor + delta

    @classmethod
    def _compute_post_run_next_dt(cls, payload: dict[str, Any], *, finished_at: datetime) -> datetime | None:
        hint = cls._normalize_schedule_hint(payload)
        delta = cls._schedule_delta(payload)
        if delta is None:
            if hint in {"once", "immediate", "auto"}:
                return None
            return None
        return finished_at + delta

    @classmethod
    def _lease_expired(cls, payload: dict[str, Any], *, now: datetime) -> bool:
        lease_expires_at = _parse_datetime_like(payload.get("lease_expires_at"))
        if lease_expires_at is None:
            return True
        return lease_expires_at <= now

    @classmethod
    def _is_due(cls, payload: dict[str, Any], *, now: datetime, force: bool = False) -> bool:
        if not cls._schedule_enabled(payload, force=force):
            return False
        status = (_safe_text(payload.get("status")) or "planned").lower()
        if status == "running" and not cls._lease_expired(payload, now=now):
            return False
        next_run_dt = cls._resolve_next_run_dt(payload, now=now)
        if next_run_dt is None:
            return force
        return next_run_dt <= now

    async def _persist_plan(self, plan_id: str, payload: dict[str, Any], *, created_at: Any = None) -> dict[str, Any]:
        return await register_artifact_async(
            {
                "artifact_id": plan_id,
                "strategy": _RETRAIN_PLAN_STRATEGY,
                "strategy_version": "p2.v3",
                "code": ",".join(str(code).strip() for code in list(payload.get("codes") or [])[:5]),
                "payload": payload,
                "created_at": created_at or payload.get("created_at") or datetime.now().isoformat(),
            }
        )

    async def _acquire_plan_lease(self, plan_artifact: dict[str, Any], *, reason: str, force: bool = False) -> dict[str, Any] | None:
        payload = _payload_from_artifact_row(plan_artifact)
        plan_id = _safe_text(plan_artifact.get("artifact_id"), payload.get("plan_id"))
        if not plan_id:
            return None
        now = datetime.now().astimezone()
        if not self._is_due(payload, now=now, force=force):
            return None
        running_owner = _safe_text(payload.get("lease_owner"))
        if (_safe_text(payload.get("status")) or "").lower() == "running" and running_owner and running_owner != self.instance_id and not self._lease_expired(payload, now=now):
            return None
        next_run_dt = self._resolve_next_run_dt(payload, now=now)
        leased_payload = {
            **payload,
            "status": "running",
            "scheduler_status": "running",
            "lease_owner": self.instance_id,
            "lease_expires_at": _isoformat(now + timedelta(seconds=self.lease_ttl_sec)),
            "heartbeat_at": _isoformat(now),
            "last_started_at": _isoformat(now),
            "scheduler_last_reason": reason,
            "next_run_at": _isoformat(next_run_dt),
        }
        await self._persist_plan(plan_id, leased_payload, created_at=plan_artifact.get("created_at"))
        return leased_payload

    async def _finalize_plan_success(
        self,
        plan_id: str,
        base_payload: dict[str, Any],
        result: dict[str, Any],
        *,
        started_at: datetime,
    ) -> dict[str, Any]:
        finished_at = datetime.now().astimezone()
        latest_artifact = await get_artifact_async(plan_id)
        latest_payload = _payload_from_artifact_row(latest_artifact or {}) if latest_artifact else {}
        result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        execution_plan = result_data.get("plan") if isinstance(result_data.get("plan"), dict) else {}
        run_payload = result_data.get("run") if isinstance(result_data.get("run"), dict) else {}
        merged = {
            **base_payload,
            **latest_payload,
            **execution_plan,
            "scheduler_status": "idle",
            "lease_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": _isoformat(finished_at),
            "last_finished_at": _isoformat(finished_at),
            "last_run_status": _safe_text(run_payload.get("status"), execution_plan.get("last_run_status"), latest_payload.get("last_run_status")) or "completed",
            "last_run_artifact_id": _safe_text(run_payload.get("artifact_id"), execution_plan.get("last_run_artifact_id"), latest_payload.get("last_run_artifact_id")),
            "failure_count": 0,
            "scheduler_last_latency_sec": round((finished_at - started_at).total_seconds(), 3),
        }
        next_run_dt = self._compute_post_run_next_dt(merged, finished_at=finished_at)
        merged["next_run_at"] = _isoformat(next_run_dt)
        await self._persist_plan(plan_id, merged, created_at=(latest_artifact or {}).get("created_at"))
        return merged

    async def _finalize_plan_failure(
        self,
        plan_id: str,
        base_payload: dict[str, Any],
        error: str,
        *,
        started_at: datetime,
    ) -> dict[str, Any]:
        finished_at = datetime.now().astimezone()
        latest_artifact = await get_artifact_async(plan_id)
        latest_payload = _payload_from_artifact_row(latest_artifact or {}) if latest_artifact else {}
        merged = {
            **base_payload,
            **latest_payload,
            "status": "failed",
            "scheduler_status": "idle",
            "lease_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": _isoformat(finished_at),
            "last_finished_at": _isoformat(finished_at),
            "last_run_status": "failed",
            "last_error": error,
            "failure_count": int(latest_payload.get("failure_count", base_payload.get("failure_count", 0)) or 0) + 1,
            "scheduler_last_latency_sec": round((finished_at - started_at).total_seconds(), 3),
        }
        next_run_dt = self._compute_post_run_next_dt(merged, finished_at=finished_at)
        merged["next_run_at"] = _isoformat(next_run_dt)
        await self._persist_plan(plan_id, merged, created_at=(latest_artifact or {}).get("created_at"))
        return merged

    async def _execute_plan(self, plan_id: str, plan_payload: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if self._executor is not None:
            return await self._executor(plan_id, plan_payload, reason)
        return await _default_execute_plan(plan_id, plan_payload, reason=reason)

    async def run_once(self, *, reason: str = "manual", force: bool = False) -> dict[str, Any]:
        async with self._run_lock:
            now = datetime.now().astimezone()
            self.last_scan_at = now
            artifacts = await self._list_plan_artifacts(limit=300)
            payloads = []
            for artifact in artifacts:
                payload = _payload_from_artifact_row(artifact)
                payloads.append(
                    {
                        "artifact": artifact,
                        "payload": payload,
                        "plan_id": _safe_text(artifact.get("artifact_id"), payload.get("plan_id")),
                        "next_run_at": _isoformat(self._resolve_next_run_dt(payload, now=now)),
                        "due": self._is_due(payload, now=now, force=force),
                    }
                )
            due_items = [item for item in payloads if item.get("due") and item.get("plan_id")]
            due_items.sort(key=lambda item: (_safe_text(item.get("next_run_at")) or "", _safe_text(item.get("plan_id")) or ""))

            executed = []
            failures = []
            skipped = []
            for item in due_items[: self.max_plans_per_run]:
                plan_id = str(item.get("plan_id") or "").strip()
                artifact = item.get("artifact") if isinstance(item.get("artifact"), dict) else {}
                leased_payload = await self._acquire_plan_lease(artifact, reason=reason, force=force)
                if leased_payload is None:
                    skipped.append({"plan_id": plan_id, "reason": "lease_not_acquired"})
                    continue
                started_at = datetime.now().astimezone()
                self.current_plan_id = plan_id
                try:
                    result = await self._execute_plan(plan_id, leased_payload, reason=reason)
                    if not isinstance(result, dict) or result.get("success") is False:
                        raise RuntimeError(
                            _safe_text(
                                (result or {}).get("error") if isinstance(result, dict) else None,
                                (result or {}).get("message") if isinstance(result, dict) else None,
                                "execute_retrain failed",
                            )
                            or "execute_retrain failed"
                        )
                    finalized = await self._finalize_plan_success(plan_id, leased_payload, result, started_at=started_at)
                    executed.append(
                        {
                            "plan_id": plan_id,
                            "status": finalized.get("status"),
                            "last_run_artifact_id": finalized.get("last_run_artifact_id"),
                            "next_run_at": finalized.get("next_run_at"),
                        }
                    )
                except Exception as exc:
                    finalized = await self._finalize_plan_failure(plan_id, leased_payload, str(exc), started_at=started_at)
                    failures.append(
                        {
                            "plan_id": plan_id,
                            "status": finalized.get("status"),
                            "next_run_at": finalized.get("next_run_at"),
                            "error": str(exc),
                        }
                    )
                finally:
                    self.current_plan_id = None

            result = {
                "scheduler": "model_retrain_scheduler",
                "instance_id": self.instance_id,
                "reason": reason,
                "scanned_count": len(payloads),
                "due_count": len(due_items),
                "executed_count": len(executed),
                "failed_count": len(failures),
                "skipped_count": len(skipped),
                "executed": executed,
                "failures": failures,
                "skipped": skipped,
                "scanned_at": _isoformat(now),
            }
            self.last_result = result
            return result

    def status(self) -> dict[str, Any]:
        return {
            "scheduler": "model_retrain_scheduler",
            "running": self._running,
            "instance_id": self.instance_id,
            "poll_interval_sec": self.poll_interval_sec,
            "lease_ttl_sec": self.lease_ttl_sec,
            "max_plans_per_run": self.max_plans_per_run,
            "current_plan_id": self.current_plan_id,
            "last_scan_at": _isoformat(self.last_scan_at),
            "last_result": self.last_result,
        }


def get_model_retrain_scheduler() -> ModelRetrainScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ModelRetrainScheduler()
    return _scheduler


__all__ = ["ModelRetrainScheduler", "get_model_retrain_scheduler"]
